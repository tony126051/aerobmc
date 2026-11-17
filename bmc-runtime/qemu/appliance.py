"""Run a simulated BMC, HTTPS gateway and persistent event collector."""
import argparse
import fcntl
from contextlib import ExitStack
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import select
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from gateway import Gateway
from verify import guest_command


def publish(path, data):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "x", opener=private_open) as stream:
        stream.write(data)
    temporary.replace(path)


def private_open(path, flags):
    import os
    return os.open(path, flags, 0o600)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait()


def provision(process, port, reader, operator, stream_id, stop):
    deadline = time.monotonic() + 20
    sent = False
    while not stop.wait(0.05) and time.monotonic() < deadline and process.poll() is None:
        conn = HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            conn.request("GET", "/v1/status", headers={"Authorization": f"Bearer {reader}",
                                                       "X-BMC-Require-Auth": "1"} if sent else {})
            response = conn.getresponse()
            response.read()
            if response.status == 200:
                if sent:
                    return True
                process.stdin.write(f"auth-enable {reader} {operator} {stream_id}\n".encode())
                process.stdin.flush()
                sent = True
        except (OSError, HTTPException):
            continue
        finally:
            conn.close()
    return False


def guest_progress(port, reader, stream_id):
    conn = HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        conn.request("GET", "/v1/events", headers={
            "Authorization": f"Bearer {reader}", "X-BMC-Require-Auth": "1",
        })
        response = conn.getresponse()
        if response.status != 200:
            return None
        journal = json.loads(response.read(65537))
        if isinstance(journal, dict) and journal.get("stream_id") == stream_id:
            progress = journal.get("control_progress")
            if type(progress) is int and progress >= 0:
                return progress
        return None
    except (OSError, HTTPException, ValueError):
        return None
    finally:
        conn.close()


def start_dbus(args, stack, url, reader_file, operator_file):
    directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="bmc-bus-", dir="/tmp")))
    config = directory / "bus.conf"
    config.write_text(
        f'<busconfig><type>session</type><listen>unix:path={directory / "bus"}</listen>'
        '<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/>'
        '<allow receive_sender="*"/><allow own="*"/></policy></busconfig>'
    )
    log = stack.enter_context((args.output / "dbus.log").open("ab"))
    daemon = subprocess.Popen([args.dbus_daemon, f"--config-file={config}", "--nofork",
                               "--print-address"], stdout=subprocess.PIPE, stderr=log, text=True)
    stack.callback(stop_process, daemon)
    if not select.select([daemon.stdout], [], [], 3)[0]:
        raise RuntimeError("D-Bus daemon did not start")
    address = daemon.stdout.readline().strip()
    if not address.startswith("unix:"):
        raise RuntimeError("D-Bus daemon did not publish a Unix address")
    command = [sys.executable, str(Path(__file__).with_name("openbmc_sensors.py")),
               url, "--ca-file", args.ca_file, "--token-file", str(reader_file),
               "--bus-address", address]
    if args.dbus_control:
        command.extend(["--operator-token-file", str(operator_file)])
    bridge = subprocess.Popen(command, stdout=log, stderr=log)
    stack.callback(stop_process, bridge)
    return address, (daemon, bridge)


def supervise(args, stop, inject_stall):
    if not args.resume:
        args.output.mkdir(mode=0o700)
    with (args.output / ".supervisor.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("output directory is already in use", file=sys.stderr)
            return 1
        identity = args.output / "appliance.json"
        if args.resume:
            failure_path = args.output / "failure.json"
            if not identity.exists() or not failure_path.exists():
                print("resume requires an appliance stopped at its restart limit", file=sys.stderr)
                return 1
            failure = json.loads(failure_path.read_text())
            if (json.loads(identity.read_text()).get("guest") != args.guest
                    or failure.get("status") != "restart_limit_reached"):
                print("resume requires the original guest type and a restart-limit record", file=sys.stderr)
                return 1
            previous_generation = failure["generation"]
            failure_path.replace(args.output / f"failure-{previous_generation}.json")
        else:
            publish(identity, json.dumps(dict(guest=args.guest)) + "\n")
            previous_generation = 0
        return run_generations(args, stop, inject_stall, previous_generation)


def run_generations(args, stop, inject_stall, previous_generation):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        backend_port = reservation.getsockname()[1]
    reader_file = args.output / "reader.token"
    operator_file = args.output / "operator.token"
    gateway = None
    dbus_address, dbus_processes = None, ()
    restart_reason = "manual_resume" if previous_generation else None
    history = args.output / "lifecycle.jsonl"

    def record(event, generation, **details):
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(event=event, generation=generation,
                                         at_unix_ms=time.time_ns() // 1_000_000, **details)) + "\n")

    command = guest_command(args.qemu, args.kernel, args.images, args.guest, backend_port)
    if previous_generation:
        record("manual_resume", previous_generation + 1, previous_generation=previous_generation)
    with ExitStack() as stack:
        for generation in range(previous_generation + 1, previous_generation + args.restart_limit + 2):
            if stop.is_set():
                return 0
            reader, operator, stream_id = secrets.token_hex(24), secrets.token_hex(24), secrets.token_hex(16)
            record("boot_attempt", generation, previous_reason=restart_reason)
            with (args.output / f"guest-{generation}.log").open("xb") as log, subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
            ) as guest:
                try:
                    if not provision(guest, backend_port, reader, operator, stream_id, stop):
                        if stop.is_set():
                            return 0
                        restart_reason = "provisioning_failed"
                        record("guest_failed", generation, reason=restart_reason)
                        continue
                    publish(reader_file, reader + "\n")
                    publish(operator_file, operator + "\n")
                    if gateway is None:
                        gateway = stack.enter_context(Gateway(("127.0.0.1", args.port), backend_port,
                                                              args.cert, args.key))
                        worker = threading.Thread(target=gateway.serve_forever)
                        worker.start()
                        stack.callback(worker.join)
                        stack.callback(gateway.shutdown)
                        monitor = subprocess.Popen([
                            sys.executable, str(Path(__file__).with_name("monitor.py")),
                            f"https://127.0.0.1:{gateway.server_port}", str(args.output / "events.sqlite"),
                            "--source", args.guest, "--ca-file", args.ca_file, "--token-file", str(reader_file),
                        ])
                        stack.callback(stop_process, monitor)
                        if args.dbus_daemon:
                            dbus_address, dbus_processes = start_dbus(
                                args, stack, f"https://127.0.0.1:{gateway.server_port}",
                                reader_file, operator_file,
                            )
                    publish(args.output / "ready.json", json.dumps(dict(
                        https_port=gateway.server_port, guest_pid=guest.pid, generation=generation,
                        stream_id=stream_id, restart_reason=restart_reason, dbus_address=dbus_address,
                    )) + "\n")
                    record("ready", generation, stream_id=stream_id)
                    failures = 0
                    previous_progress = None
                    probe_reason = None
                    next_probe = time.monotonic() + 1
                    while guest.poll() is None and not stop.wait(0.1):
                        if monitor.poll() is not None:
                            raise RuntimeError("event collector exited")
                        if any(process.poll() is not None for process in dbus_processes):
                            raise RuntimeError("D-Bus service exited")
                        if inject_stall.is_set():
                            inject_stall.clear()
                            guest.stdin.write(b"test-control-stall\n")
                            guest.stdin.flush()
                        if time.monotonic() >= next_probe:
                            progress = guest_progress(backend_port, reader, stream_id)
                            if progress is None:
                                reason = "management_unresponsive"
                            elif previous_progress is not None and progress <= previous_progress:
                                reason = "control_stalled"
                            else:
                                reason = None
                                previous_progress = progress
                            failures = 0 if reason is None else (failures + 1 if reason == probe_reason else 1)
                            probe_reason = reason
                            next_probe = time.monotonic() + 1
                            if failures == 3:
                                break
                    if guest.poll() is not None:
                        restart_reason = "guest_exited"
                    elif not stop.is_set():
                        restart_reason = probe_reason
                        print(f"guest {generation}: three failed health probes ({restart_reason})", file=sys.stderr, flush=True)
                    if not stop.is_set():
                        record("guest_failed", generation, reason=restart_reason)
                finally:
                    (args.output / "ready.json").unlink(missing_ok=True)
                    stop_process(guest)
            if stop.wait(0.2):
                return 0
    record("restart_limit_reached", generation, reason=restart_reason)
    publish(args.output / "failure.json", json.dumps(dict(
        status="restart_limit_reached", generation=generation, reason=restart_reason,
    )) + "\n")
    print("guest restart limit reached", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("guest", choices=("linux", "arceos"))
    parser.add_argument("qemu")
    parser.add_argument("kernel")
    parser.add_argument("images", type=Path)
    parser.add_argument("output", type=Path, help="new directory for credentials, logs and SQLite archive")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--restart-limit", type=int, default=3)
    parser.add_argument("--dbus-daemon", help="enable a private OpenBMC D-Bus using this executable")
    parser.add_argument("--dbus-control", action="store_true", help="enable power operations on the private bus")
    parser.add_argument("--resume", action="store_true", help="resume an existing appliance after its restart limit")
    parser.add_argument("--test-control-stall", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.restart_limit < 0:
        parser.error("--restart-limit must be nonnegative")
    if args.dbus_control and not args.dbus_daemon:
        parser.error("--dbus-control requires --dbus-daemon")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    inject_stall = threading.Event()
    if args.test_control_stall:
        signal.signal(signal.SIGUSR1, lambda *_: inject_stall.set())
    return supervise(args, stop, inject_stall)


if __name__ == "__main__":
    sys.exit(main())

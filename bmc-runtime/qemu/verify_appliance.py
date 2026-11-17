"""Boot both appliance variants and kill the guest to exercise automatic recovery."""
import argparse
import asyncio
from contextlib import closing
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import signal
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time


def wait_until(condition):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("timed out waiting for appliance")


def verify(guest, qemu, kernel, images, output, cert, key, daemon=None):
    directory = output / guest
    command = [sys.executable, str(Path(__file__).with_name("appliance.py")), guest,
               qemu, kernel, images, str(directory), "--port", "0", "--cert", str(cert),
               "--key", str(key), "--ca-file", str(cert), "--test-control-stall"]
    if daemon:
        command.extend(["--dbus-daemon", daemon, "--dbus-control"])
    context = ssl.create_default_context(cafile=str(cert))

    def ready(generation):
        path = directory / "ready.json"
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        return value if value["generation"] == generation else None

    def request(port, token, method, path, body=None):
        with closing(HTTPSConnection("127.0.0.1", port, context=context, timeout=2)) as conn:
            conn.request(method, path, body=body,
                         headers={"Authorization": f"Bearer {token}", "X-BMC-Action": "1"})
            reply = conn.getresponse()
            data = reply.read()
            return reply.status, json.loads(data) if data else None

    def archived(generation, kind):
        if not (directory / "events.sqlite").exists():
            return False
        with closing(sqlite3.connect(directory / "events.sqlite")) as db:
            return any(json.loads(row[0])["kind"] == kind for row in db.execute(
                "SELECT payload FROM events WHERE generation = ?", (generation,),
            ))

    async def dbus_start(address):
        from dbus_next.aio import MessageBus
        from dbus_next.errors import DBusError
        from openbmc_sensors import AVAILABILITY, HOST_PATH, HOST_STATE, SERVICE, TRANSITION_PREFIX

        bus = await MessageBus(bus_address=address).connect()
        try:
            while True:
                try:
                    node = await bus.introspect(SERVICE, HOST_PATH)
                except DBusError:
                    await asyncio.sleep(0.05)
                    continue
                host = bus.get_proxy_object(SERVICE, HOST_PATH, node)
                if await host.get_interface(AVAILABILITY).get_available():
                    break
                await asyncio.sleep(0.05)
            await host.get_interface(HOST_STATE).set_requested_host_transition(TRANSITION_PREFIX + "On")
            return bus.unique_name
        finally:
            bus.disconnect()

    def exercise(port, generation, address=None):
        token = (directory / "operator.token").read_text().strip()
        if address:
            asyncio.run(asyncio.wait_for(dbus_start(address), timeout=5))
        else:
            code, _ = request(port, token, "POST", "/redfish/v1/Systems/Host/Actions/ComputerSystem.Reset",
                              '{"ResetType":"On"}')
            assert code == 202
        wait_until(lambda: archived(generation, "power_locked"))
        return token

    with (output / f"{guest}.log").open("wb") as log, subprocess.Popen(
        command, stdout=log, stderr=subprocess.STDOUT,
    ) as process:
        try:
            first = wait_until(lambda: ready(1))
            duplicate = subprocess.run([*command, "--resume"], capture_output=True, timeout=5)
            assert duplicate.returncode == 1 and b"already in use" in duplicate.stderr
            port = first["https_port"]
            address = first.get("dbus_address")
            assert bool(address) == bool(daemon)
            old_token = exercise(port, 1, address)
            os.kill(first["guest_pid"], signal.SIGKILL)
            second = wait_until(lambda: ready(2))
            assert port == second["https_port"] and first["stream_id"] != second["stream_id"]
            assert second["restart_reason"] == "guest_exited"
            assert request(port, old_token, "POST", "/v1/actions/start")[0] == 401
            assert second.get("dbus_address") == address
            token = exercise(port, 2, address)
            assert token != old_token
            os.kill(second["guest_pid"], signal.SIGSTOP)
            third = wait_until(lambda: ready(3))
            assert third["restart_reason"] == "management_unresponsive"
            assert third["guest_pid"] != second["guest_pid"]
            assert third["https_port"] == port and third["stream_id"] != second["stream_id"]
            assert request(port, token, "POST", "/v1/actions/start")[0] == 401
            operator = exercise(port, 3)
            assert operator != token
            process.send_signal(signal.SIGUSR1)
            code, before = request(port, operator, "GET", "/v1/events")
            assert code == 200
            time.sleep(0.3)
            code, frozen = request(port, operator, "GET", "/v1/events")
            assert code == 200
            time.sleep(0.3)
            assert request(port, operator, "GET", "/v1/events") == (200, frozen)
            assert before["stream_id"] == frozen["stream_id"]
            fourth = wait_until(lambda: ready(4))
            assert fourth["restart_reason"] == "control_stalled"
            assert fourth["https_port"] == port and fourth["stream_id"] != third["stream_id"]
            assert request(port, operator, "POST", "/v1/actions/start")[0] == 401
            assert exercise(port, 4) != operator
            with closing(sqlite3.connect(directory / "events.sqlite")) as db:
                assert db.execute("SELECT COUNT(*) FROM alerts WHERE kind='stream_reset'").fetchone()[0] == 3
            os.kill(fourth["guest_pid"], signal.SIGKILL)
            assert process.wait(timeout=10) == 1
            failure = json.loads((directory / "failure.json").read_text())
            assert failure == dict(status="restart_limit_reached", generation=4, reason="guest_exited")
            lifecycle = [json.loads(line) for line in (directory / "lifecycle.jsonl").read_text().splitlines()]
            assert [entry["generation"] for entry in lifecycle if entry["event"] == "boot_attempt"] == [1, 2, 3, 4]
            assert [entry["reason"] for entry in lifecycle if entry["event"] == "guest_failed"] == [
                "guest_exited", "management_unresponsive", "control_stalled", "guest_exited",
            ]
            assert lifecycle[-1]["event"] == "restart_limit_reached"
            try:
                request(port, operator, "GET", "/v1/status")
            except ConnectionRefusedError:
                pass
            else:
                raise AssertionError("HTTPS still accepting connections after restart limit")
            assert archived(4, "power_locked")
            report = dict(guest_restarted=True, same_https_port=True, old_token_status=401,
                          restart_reasons=[second["restart_reason"], third["restart_reason"],
                                           fourth["restart_reason"]],
                          http_responded_during_control_stall=True,
                          archived_power_fault_generations=[1, 2, 3, 4], terminal_failure=failure)
            if daemon:
                report["dbus_control_after_token_rotation"] = True
            (output / f"{guest}-appliance.json").write_text(json.dumps(report, indent=2) + "\n")
        finally:
            if process.poll() is None:
                process.terminate()
                assert process.wait(timeout=10) == 0
        assert not (directory / "ready.json").exists()
    with (output / f"{guest}-resume.log").open("wb") as log, subprocess.Popen(
        [*command, "--resume"], stdout=log, stderr=subprocess.STDOUT,
    ) as process:
        try:
            resumed = wait_until(lambda: ready(5))
            assert resumed["restart_reason"] == "manual_resume"
            assert request(resumed["https_port"], operator, "POST", "/v1/actions/start")[0] == 401
            assert exercise(resumed["https_port"], 5) != operator
            assert json.loads((directory / "failure-4.json").read_text()) == failure
            assert not (directory / "failure.json").exists()
            assert all(archived(generation, "power_locked") for generation in range(1, 6))
            report.update(manual_resume=True, archived_power_fault_generations=list(range(1, 6)),
                          concurrent_resume_rejected=True)
            (output / f"{guest}-appliance.json").write_text(json.dumps(report, indent=2) + "\n")
        finally:
            if process.poll() is None:
                process.terminate()
            assert process.wait(timeout=10) == 0
    # Keep the archive and logs, remove temporary test credentials.
    for name in ("reader.token", "operator.token"):
        (directory / name).unlink()
    print(f"{guest}: appliance startup, HTTPS, archive and automatic guest recovery verified", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "kernel", "images", "destination"):
        parser.add_argument(name)
    parser.add_argument("--dbus-daemon")
    args = parser.parse_args()
    qemu, kernel, images, destination = args.qemu, args.kernel, args.images, args.destination
    output = Path(destination)
    output.mkdir()
    with tempfile.TemporaryDirectory(prefix="bmc-appliance-cert-") as tmp:
        cert, key, config = (Path(tmp) / name for name in ("cert.pem", "key.pem", "openssl.cnf"))
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                          "[dn]\nCN=BMC simulation\n[ext]\nsubjectAltName=IP:127.0.0.1\n")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-config", str(config), "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        for guest in ("linux", "arceos"):
            verify(guest, qemu, kernel, images, output, cert, key, args.dbus_daemon)

"""Exercise HTTPS and authenticated GPIO control against both QEMU guests."""
from contextlib import closing, contextmanager, ExitStack
from http.client import HTTPSConnection
import json
from pathlib import Path
import secrets
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from gateway import Gateway
from verify import run


@contextmanager
def serve(gateway):
    with gateway:
        worker = threading.Thread(target=gateway.serve_forever)
        worker.start()
        try:
            yield gateway.server_port
        finally:
            gateway.shutdown()
            worker.join()


def verify(qemu, kernel, images, output, guest, cert, key, check_release=False):
    directory = output / guest
    directory.mkdir()
    recovery = {}
    archive = directory / "events.sqlite"

    def collect(tls_port, token):
        with tempfile.TemporaryDirectory(prefix="bmc-reader-") as tmp:
            path = Path(tmp) / "reader"
            path.touch(mode=0o600)
            path.write_text(token)
            result = subprocess.run([
                sys.executable, str(Path(__file__).with_name("monitor.py")),
                f"https://127.0.0.1:{tls_port}", str(archive), "--source", guest,
                "--once", "--ca-file", str(cert), "--token-file", str(path),
            ], capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

    def workload(vm, fixture, request, wait_for, port):
        try:
            gateway = Gateway(("127.0.0.1", 0), port, cert, key)
        except RuntimeError:
            pass
        else:
            gateway.server_close()
            raise AssertionError("gateway accepted an unprovisioned backend")
        reader, operator = secrets.token_hex(24), secrets.token_hex(24)
        vm.stdin.write(f"auth-enable {reader} {operator} {secrets.token_hex(16)}\n".encode())
        vm.stdin.flush()
        deadline = time.monotonic() + 3
        while request()[0] != 401:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        reader_headers = {"Authorization": f"Bearer {reader}"}
        operator_headers = {"Authorization": f"Bearer {operator}", "X-BMC-Action": "1"}

        tls_port = gateway_lifetime.enter_context(serve(Gateway(("127.0.0.1", 0), port, cert, key)))
        context = ssl.create_default_context(cafile=str(cert))

        def https(method="GET", path="/v1/status", headers=None, body=None):
            with closing(HTTPSConnection("127.0.0.1", tls_port, context=context, timeout=2)) as conn:
                conn.request(method, path, body=body, headers=headers or {})
                reply = conn.getresponse()
                data = reply.read()
                return reply.status, json.loads(data) if data else None, reply.headers

        for host, trust in (("127.0.0.1", ssl.create_default_context()), ("localhost", context)):
            with closing(HTTPSConnection(host, tls_port, context=trust, timeout=2)) as conn:
                try:
                    conn.request("GET", "/v1/status")
                except ssl.SSLCertVerificationError:
                    pass
                else:
                    raise AssertionError("untrusted certificate or wrong hostname accepted")

        def expect_power(state):
            deadline = time.monotonic() + 2
            while True:
                code, snapshot, _ = https(headers=reader_headers)
                assert code in (200, 502, 503, 504), (code, snapshot)
                if code == 200 and snapshot["power"]["state"] == state:
                    return
                assert time.monotonic() < deadline
                time.sleep(0.01)

        assert https()[0] == 401
        assert https(headers={"Authorization": "Bearer invalid"})[0] == 401
        assert https(path="/redfish/v1/", headers=reader_headers)[0] == 200
        with closing(HTTPSConnection("127.0.0.1", tls_port, context=context, timeout=2)) as conn:
            conn.request("GET", "/v1/status", headers={**reader_headers, "Connection": "keep-alive"})
            reply = conn.getresponse()
            assert reply.status == 200 and reply.getheader("Connection") == "keep-alive"
            reply.read()
            connection = conn.sock
            assert connection is not None
            # The backend idle timeout is shorter than the TLS timeout.
            time.sleep(1.3)
            conn.request("GET", "/v1/status", headers={**reader_headers, "Connection": "keep-alive"})
            reply = conn.getresponse()
            assert reply.status == 200
            reply.read()
            assert conn.sock is connection
            conn.request("GET", "/v1/status", headers={"Connection": "keep-alive"})
            reply = conn.getresponse()
            assert reply.status == 401 and reply.getheader("Connection") == "close"
            reply.read()
            assert conn.sock is None
        assert https("POST", "/v1/actions/start", dict(reader_headers, **{"X-BMC-Action": "1"}))[0] == 403
        assert fixture.outputs() & 1 == 0
        code, result, _ = https("POST", "/v1/actions/start", operator_headers)
        assert code == 202
        expect_power("starting")
        fixture.level(5, True)
        expect_power("on")
        assert fixture.outputs() & 1 == 1
        reset = "/redfish/v1/Systems/Host/Actions/ComputerSystem.Reset"
        code, task, headers = https("POST", reset, operator_headers, b'{"ResetType":"ForceOff"}')
        assert code == 202
        expect_power("off")
        assert fixture.outputs() & 1 == 0
        assert https(path=headers["Location"], headers=reader_headers)[0] == 204
        assert https(path=task["@odata.id"], headers=reader_headers)[1]["TaskState"] == "Completed"
        fixture.level(5, False)
        with tempfile.TemporaryDirectory(prefix="bmc-manage-") as tmp:
            token_file = Path(tmp) / "operator"
            token_file.touch(mode=0o600)
            token_file.write_text(operator)
            command = [sys.executable, str(Path(__file__).with_name("manage.py")),
                       f"https://127.0.0.1:{tls_port}", "--ca-file", str(cert),
                       "--token-file", str(token_file)]
            def expect_gpio(enabled):
                deadline = time.monotonic() + 2
                while bool(fixture.outputs() & 1) != enabled:
                    assert time.monotonic() < deadline
                    time.sleep(0.005)

            with subprocess.Popen([*command, "On"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as process:
                try:
                    expect_gpio(True)
                    fixture.level(5, True)
                    stdout, stderr = process.communicate(timeout=5)
                    assert process.returncode == 0, stderr
                    cli_started = json.loads(stdout)
                    assert cli_started["power"] == "On" and fixture.outputs() & 1
                finally:
                    if process.poll() is None:
                        process.kill()
            cycle_command = [*command, "--idempotency-key", "cli-cycle", "PowerCycle"]
            with subprocess.Popen(cycle_command, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as process:
                try:
                    expect_gpio(False)
                    assert fixture.outputs() & 1 == 0 and process.poll() is None
                    fixture.level(5, False)
                    expect_gpio(True)
                    assert process.poll() is None
                    fixture.level(5, True)
                    stdout, stderr = process.communicate(timeout=5)
                    assert process.returncode == 0, stderr
                    cli_cycle = json.loads(stdout)
                    assert cli_cycle["power"] == "On" and fixture.outputs() & 1
                finally:
                    if process.poll() is None:
                        process.kill()
            replay = subprocess.run(cycle_command, capture_output=True, text=True, check=True)
            assert json.loads(replay.stdout)["task"] == cli_cycle["task"] and fixture.outputs() & 1
            stopped = subprocess.run([*command, "ForceOff"], capture_output=True, text=True, check=True)
            assert json.loads(stopped.stdout)["power"] == "Off" and fixture.outputs() & 1 == 0
            fixture.level(5, False)
            failed = subprocess.run([*command, "On"], capture_output=True, text=True)
            assert failed.returncode == 2, failed.stderr
            failure_report = json.loads(failed.stdout)
            assert any(entry["Message"] == "power_locked" for entry in failure_report["events"])
            assert fixture.outputs() & 1 == 0
        recovery["first"] = collect(tls_port, reader)
        assert recovery["first"]["archived"] >= 3
        if check_release:
            vm.stdin.write(b"test-control-stall\n")
            vm.stdin.flush()
            # Allow the UART reader to consume the command before sampling.
            time.sleep(0.3)
            code, before, _ = https(path="/v1/events", headers=reader_headers)
            assert code == 200
            time.sleep(0.2)
            code, after, _ = https(path="/v1/events", headers=reader_headers)
            assert code == 200 and after["control_progress"] > before["control_progress"]
        report = dict(verified_certificate=True, rejected_untrusted_certificate=True,
                      rejected_hostname_mismatch=True, anonymous=401, reader_write=403,
                          operator_write=202, final_gpio_power=0,
                      cli_started=cli_started, cli_cycle=cli_cycle, cli_cycle_replay=True,
                      cli_failure=failure_report)
        (directory / "https.json").write_text(json.dumps(report, indent=2) + "\n")
        if check_release:
            (directory / "release-guard.json").write_text(json.dumps({
                "control_progress_before": before["control_progress"],
                "control_progress_after": after["control_progress"],
            }, indent=2) + "\n")
        recovery["tls_port"] = tls_port
        recovery["backend_port"] = port
        recovery["operator"] = operator

    def rebooted(vm, fixture, request, wait_for, port):
        tls_port = recovery.pop("tls_port")
        context = ssl.create_default_context(cafile=str(cert))
        with closing(HTTPSConnection("127.0.0.1", tls_port, context=context, timeout=2)) as conn:
            conn.request("POST", "/v1/actions/start", headers={
                "X-BMC-Action": "1", "Authorization": "Bearer " + recovery.pop("operator"),
            })
            response = conn.getresponse()
            code = response.status
            response.read()
        assert code == 401 and fixture.outputs() & 1 == 0
        recovery["unprovisioned_write"] = code
        reader, operator = secrets.token_hex(24), secrets.token_hex(24)
        vm.stdin.write(f"auth-enable {reader} {operator} {secrets.token_hex(16)}\n".encode())
        vm.stdin.flush()
        deadline = time.monotonic() + 3
        while request()[0] != 401:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        headers = {"Authorization": f"Bearer {reader}"}
        # Advance past the prior cursor and overwrite its anchor before reconnecting.
        for _ in range(12):
            for temperature in (85000, 55000):
                vm.stdin.write(f"temperature 1 {temperature}\n".encode())
                vm.stdin.flush()
                deadline = time.monotonic() + 2
                while request(headers=headers)[1]["sensors"][1]["temperature_millicelsius"] != temperature:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
        recovery["after_reboot"] = collect(tls_port, reader)
        assert recovery["after_reboot"]["generation"] == 2
        assert recovery["after_reboot"]["archived"] == 32
        assert [a["kind"] for a in recovery["after_reboot"]["alerts"]] == ["stream_reset", "events_missing"]
        assert collect(tls_port, reader)["archived"] == 0


    with ExitStack() as gateway_lifetime:
        with tempfile.TemporaryDirectory(prefix="bmc-tls-", dir="/tmp") as sockets:
            run(qemu, kernel, directory, guest, Path(sockets), images=images, workload=workload)
        reboot_dir = directory / "reboot"
        reboot_dir.mkdir()
        with tempfile.TemporaryDirectory(prefix="bmc-tls-", dir="/tmp") as sockets:
            run(qemu, kernel, reboot_dir, guest, Path(sockets), images=images, workload=rebooted,
                port=recovery.pop("backend_port"))
    with closing(sqlite3.connect(archive)) as db:
        assert dict(db.execute("SELECT generation, COUNT(*) FROM events GROUP BY generation")) == {
            1: recovery["first"]["archived"], 2: 32,
        }
    (directory / "recovery.json").write_text(json.dumps(recovery, indent=2) + "\n")
    print(f"{guest}: HTTPS trust, role enforcement and Redfish GPIO control verified", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("qemu", "kernel", "images", "output"):
        parser.add_argument(argument)
    parser.add_argument("--check-release", action="store_true")
    args = parser.parse_args()
    qemu, kernel, images, output = args.qemu, args.kernel, args.images, args.output
    output = Path(output)
    output.mkdir()
    with tempfile.TemporaryDirectory(prefix="bmc-cert-") as temporary:
        cert, key = (Path(temporary) / name for name in ("cert.pem", "key.pem"))
        config = Path(temporary) / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                          "[dn]\nCN=BMC simulation\n[ext]\nsubjectAltName=IP:127.0.0.1\n")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "1", "-config", str(config), "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        for guest in ("linux", "arceos"):
            verify(qemu, kernel, Path(images), output, guest, cert, key, args.check_release)


if __name__ == "__main__":
    main()

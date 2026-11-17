"""Measure authenticated HTTPS control with continuous event archival."""
import argparse
from contextlib import closing
import hashlib
from http.client import HTTPSConnection, HTTPException
import json
from pathlib import Path
import secrets
import platform
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time

from appliance import provision, publish, stop_process
from benchmark import DEADLINE_MS, SLOT_S, cpu_seconds, percentile, trial
from verify import run
from threading import Event


def measure(vm, fixture, backend_port, directory, cert, key, count, rate, keep_alive):
    reader, operator = (secrets.token_hex(24) for _ in range(2))
    stream = secrets.token_hex(16)
    assert provision(vm, backend_port, reader, operator, stream, Event())
    scripts = Path(__file__).resolve().parent
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    context = ssl.create_default_context(cafile=str(cert))
    archive = directory / "events.sqlite"

    def request(method="GET", path="/v1/status", headers=None, session=None):
        token = reader if method == "GET" else operator
        outgoing = {"Authorization": "Bearer " + token, **(headers or {})}
        if session is not None:
            session.request(method, path, headers=outgoing)
            reply = session.getresponse()
            return reply.status, json.loads(reply.read())
        with closing(HTTPSConnection("127.0.0.1", port, context=context, timeout=0.5)) as conn:
            conn.request(method, path, headers=outgoing)
            reply = conn.getresponse()
            return reply.status, json.loads(reply.read())

    def wait_for(condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                code, value = request()
                if code == 200 and condition(value):
                    return value
            except (OSError, HTTPException):
                pass
            time.sleep(0.01)
        raise RuntimeError("HTTPS did not reach expected state")

    with tempfile.TemporaryDirectory(prefix="bmc-bench-token-") as tmp:
        token_file = Path(tmp) / "reader.token"
        publish(token_file, reader)
        with (directory / "services.log").open("wb") as log:
            gateway = subprocess.Popen([sys.executable, str(scripts / "gateway.py"),
                                        "--backend-port", str(backend_port), "--port", str(port),
                                        "--cert", str(cert), "--key", str(key)], stdout=log, stderr=log)
            try:
                wait_for(lambda _: True)
                monitor = subprocess.Popen([sys.executable, str(scripts / "monitor.py"),
                                            f"https://127.0.0.1:{port}", str(archive),
                                            "--ca-file", str(cert), "--token-file", str(token_file)],
                                           stdout=log, stderr=log)
                try:
                    processes = {"gateway": gateway, "collector": monitor}
                    start = {name: cpu_seconds(proc.pid) for name, proc in processes.items()}
                    session = HTTPSConnection("127.0.0.1", port, context=context, timeout=0.5) if keep_alive else None
                    result = trial(vm, fixture, request, wait_for, count, rate, session)
                    result["host_cpu_seconds"] = {
                        name: cpu_seconds(proc.pid) - start[name] for name, proc in processes.items()
                    }
                    deadline = time.monotonic() + 3
                    while True:
                        with closing(sqlite3.connect(archive)) as db:
                            faults = db.execute(
                                "SELECT COUNT(*) FROM events WHERE json_extract(payload,'$.kind')='power_good_lost'"
                            ).fetchone()[0]
                            gaps = db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
                        if faults == count:
                            break
                        assert time.monotonic() < deadline, f"archived {faults}/{count} faults"
                        time.sleep(0.05)
                    assert gaps == 0
                    result["archived_faults"] = faults
                    return result
                finally:
                    stop_process(monitor)
            finally:
                stop_process(gateway)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qemu")
    parser.add_argument("kernel")
    parser.add_argument("images", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--http-hz", type=int, default=25)
    parser.add_argument("--samples-per-boot", type=int, default=50)
    parser.add_argument("--keep-alive", action="store_true")
    args = parser.parse_args()
    if min(args.http_hz, args.samples_per_boot) < 1:
        parser.error("rate and sample count must be positive")
    args.output.mkdir()
    manifest = dict(host=platform.platform(), tls=ssl.OPENSSL_VERSION,
                    qemu=subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0],
                    http_hz=args.http_hz, keep_alive=args.keep_alive,
                    samples_per_boot=args.samples_per_boot, slot_seconds=SLOT_S,
                    sha256={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (Path(args.kernel), args.images / "linux.cpio", args.images / "arceos.bin")})
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    results = {"linux": [], "arceos": []}
    with tempfile.TemporaryDirectory(prefix="bmc-https-bench-", dir="/tmp") as tmp:
        cert, key, config = (Path(tmp) / name for name in ("cert.pem", "key.pem", "openssl.cnf"))
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                          "[dn]\nCN=BMC simulation\n[ext]\nsubjectAltName=IP:127.0.0.1\n")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-config", str(config), "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        for index, guest in enumerate(("linux", "arceos", "arceos", "linux"), 1):
            directory = args.output / f"{index}-{guest}"
            directory.mkdir()

            def workload(vm, fixture, request, wait_for, port):
                result = measure(vm, fixture, port, directory, cert, key,
                                 args.samples_per_boot, args.http_hz, args.keep_alive)
                (directory / "measurements.json").write_text(json.dumps(result, indent=2) + "\n")
                results[guest].append(result)

            with tempfile.TemporaryDirectory(prefix="bmc-bench-", dir="/tmp") as sockets:
                run(args.qemu, args.kernel, directory, guest, Path(sockets), images=args.images, workload=workload)
            print(f"{guest}: boot {index} verified", flush=True)
    summary = {}
    for guest, boots in results.items():
        latencies = [row["latency_ms"] for boot in boots for row in boot["samples"]]
        queries = [query for boot in boots for query in boot["queries"]]
        successful = [query["elapsed_ms"] for query in queries if query["ok"]]
        summary[guest] = dict(
            samples=len(latencies), shutdown_p99_ms=percentile(latencies, 0.99),
            deadline_ms=DEADLINE_MS, missed_deadline=sum(x > DEADLINE_MS for x in latencies),
            http_requests=len(queries), http_errors=len(queries) - len(successful),
            reused_connection=sum(query["reused_connection"] for query in queries),
            https_p99_ms=percentile(successful, 0.99) if successful else None,
            qemu_cpu_seconds=sum(boot["qemu_cpu_seconds"] for boot in boots),
            host_cpu_seconds={name: sum(boot["host_cpu_seconds"][name] for boot in boots)
                              for name in ("gateway", "collector")},
            archived_faults=sum(boot["archived_faults"] for boot in boots),
        )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

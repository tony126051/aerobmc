"""Measure host-observed power-good loss while querying the current Runtime."""
import argparse
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import math
from pathlib import Path
import platform
import secrets
import subprocess
import tempfile
import threading
import time

from verify import run

SLOT_S = 0.35
DEADLINE_MS = 65
POLL_S = 0.001


def sleep_until(target):
    time.sleep(max(0, target - time.monotonic()))


def cpu_seconds(pid):
    value = subprocess.check_output(["ps", "-p", str(pid), "-o", "time="], text=True).strip()
    seconds = 0.0
    for field in value.split(":"):
        seconds = seconds * 60 + float(field)
    return seconds


def trial(vm, fixture, request, wait_for, count, http_hz, session):
    fixture.tach(8)
    wait_for(lambda value: value["fan"]["protected"])
    rows, queries = [], []
    stop = threading.Event()
    start = time.monotonic() + 0.1
    http_lock = threading.Lock()

    def query_load():
        for index in range(math.ceil(count * SLOT_S * http_hz)):
            scheduled = start + index / http_hz
            if stop.wait(max(0, scheduled - time.monotonic())):
                return
            try:
                with http_lock:
                    issued = time.monotonic()
                    connected = session is not None and session.sock is not None
                    headers = {"Connection": "keep-alive"} if session is not None else None
                    status, body = request(headers=headers, session=session)
                error = None if status == 200 and "revision" in body else f"http_{status}"
            except (OSError, ValueError, HTTPException) as exc:
                error = f"{type(exc).__name__}: {exc}"
                if session is not None:
                    session.close()
            queries.append(dict(ok=error is None, error=error, lag_ms=(issued - scheduled) * 1_000,
                                reused_connection=connected,
                                elapsed_ms=(time.monotonic() - issued) * 1_000))

    worker = threading.Thread(target=query_load)
    cpu_start = cpu_seconds(vm.pid)
    worker.start()
    try:
        for index in range(count):
            target = start + index * SLOT_S
            sleep_until(target)
            headers = {"X-BMC-Action": "1", "Idempotency-Key": secrets.token_hex(16)}
            for start_attempt in range(3):
                with http_lock:
                    status, body = request("POST", "/v1/actions/start", headers)
                if status not in (502, 503, 504):
                    break
                time.sleep(POLL_S)
            assert status == 202 and body["queued"], (status, body)
            while not fixture.outputs() & 1:
                assert time.monotonic() < target + SLOT_S, "GPIO never enabled"
                time.sleep(POLL_S)
            fixture.level(5, True)
            with http_lock:
                running = wait_for(lambda value: value["power"]["state"] == "on")
            assert fixture.outputs() & 1
            injected = time.monotonic()
            fixture.level(5, False)
            while fixture.outputs() & 1:
                assert time.monotonic() - injected < 0.5, "GPIO remained enabled"
                time.sleep(POLL_S)
            latency = (time.monotonic() - injected) * 1_000
            with http_lock:
                lost = wait_for(lambda value: value["power"]["state"] == "power_good_lost")
            assert not lost["power"]["enabled"] and not lost["watchdog"]["armed"]
            assert lost["watchdog"]["trips"] == 0
            for attempt in range(3):
                try:
                    with http_lock:
                        status, journal = request(path="/v1/events")
                    if status in (502, 503, 504) and attempt < 2:
                        time.sleep(POLL_S)
                        continue
                    break
                except (OSError, HTTPException):
                    if attempt == 2:
                        raise
                    time.sleep(POLL_S)
            assert status == 200
            transitions = [event["kind"] for event in journal["events"]
                           if running["revision"] < event["revision"] <= lost["revision"]]
            assert transitions == ["power_good_lost"], transitions
            rows.append(dict(latency_ms=latency, events=transitions, start_retries=start_attempt,
                             trial_start_lag_ms=(injected - target) * 1_000))
            assert time.monotonic() < target + SLOT_S, "trial overran the offered schedule"
        sleep_until(start + count * SLOT_S)
        worker.join()
    finally:
        stop.set()
        worker.join()
        if session is not None:
            session.close()
    wall = time.monotonic() - start
    return dict(samples=rows, queries=queries, qemu_cpu_seconds=cpu_seconds(vm.pid) - cpu_start,
                wall_seconds=wall)


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qemu")
    parser.add_argument("kernel")
    parser.add_argument("images", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--samples-per-boot", type=int, default=50)
    parser.add_argument("--http-hz", type=int, default=25, help="background status requests per second (0 disables them)")
    parser.add_argument("--keep-alive", action="store_true", help="reuse the background status connection")
    args = parser.parse_args()
    if args.samples_per_boot < 1:
        parser.error("--samples-per-boot must be positive")
    if args.http_hz < 0:
        parser.error("--http-hz must be nonnegative")
    args.output.mkdir()
    manifest = dict(host=platform.platform(), python=platform.python_version(),
                    qemu=subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0],
                    platform_commit=(args.images / "platform-commit.txt").read_text().strip(),
                    sha256={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (Path(args.kernel), args.images / "linux.cpio", args.images / "arceos.bin")},
                    http_hz=args.http_hz, keep_alive=args.keep_alive,
                    slot_ms=SLOT_S * 1_000, gpio_poll_ms=POLL_S * 1_000,
                    deadline_ms=DEADLINE_MS, samples_per_boot=args.samples_per_boot)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    runs = {"linux": [], "arceos": []}
    for index, guest in enumerate(("linux", "arceos", "arceos", "linux"), 1):
        directory = args.output / f"{index}-{guest}"
        directory.mkdir()
        with tempfile.TemporaryDirectory(prefix="bmc-perf-", dir="/tmp") as sockets:
            def workload(vm, fixture, request, wait_for, port):
                session = HTTPConnection("127.0.0.1", port, timeout=0.5) if args.keep_alive else None
                result = trial(vm, fixture, request, wait_for, args.samples_per_boot, args.http_hz, session)
                (directory / "measurements.json").write_text(json.dumps(result, indent=2) + "\n")
                runs[guest].append(result)

            run(args.qemu, args.kernel, directory, guest, Path(sockets), images=args.images, workload=workload)
        print(f"{guest}: boot {index} verified", flush=True)
    summary = {}
    for guest, boots in runs.items():
        values = [row["latency_ms"] for boot in boots for row in boot["samples"]]
        queries = [query for boot in boots for query in boot["queries"]]
        success_times = [query["elapsed_ms"] for query in queries if query["ok"]]
        summary[guest] = dict(
            samples=len(values), p50_ms=percentile(values, 0.5), p99_ms=percentile(values, 0.99),
            max_ms=max(values), missed_deadline=sum(value > DEADLINE_MS for value in values),
            http_requests=len(queries), http_errors=sum(not q["ok"] for q in queries),
            http_reused_connection=sum(q["reused_connection"] for q in queries),
            http_issue_lag_p99_ms=percentile([q["lag_ms"] for q in queries], 0.99) if queries else None,
            http_success_latency_p99_ms=percentile(success_times, 0.99) if success_times else None,
            qemu_cpu_seconds=sum(boot["qemu_cpu_seconds"] for boot in boots),
            wall_seconds=sum(boot["wall_seconds"] for boot in boots),
        )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

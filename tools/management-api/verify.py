"""Boot both guests and validate the read-only API through QEMU networking."""

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import time


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        assert response.getheader("Content-Type") == "application/json"
        return response.status, json.loads(response.read()), response.getheader("Allow")
    finally:
        connection.close()


def verify(qemu, guest, args, output):
    # Obtain an unused loopback port for QEMU's host forwarding.
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    command = [
        qemu, "-M", "virt,gic-version=2", "-accel", "tcg", "-cpu", "cortex-a72",
        "-smp", "1", "-m", "256M", "-display", "none", "-serial", "stdio",
        "-monitor", "none", "-no-reboot", "-global", "virtio-mmio.force-legacy=false",
        "-netdev", f"user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:5555",
        "-device", "virtio-net-device,netdev=net0",
    ]
    if guest == "linux":
        command += [
            "-kernel", str(args.kernel), "-initrd", str(args.initramfs),
            "-append", "console=ttyAMA0 rdinit=/init quiet",
        ]
    else:
        command += ["-kernel", str(args.arceos_image)]

    (output / f"{guest}-command.json").write_text(json.dumps(command, indent=2) + "\n")
    log = output / f"{guest}.log"
    with log.open("wb") as stream, subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT
    ) as process:
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    code, healthy, _ = request(port, "GET", "/v1/status")
                    if code == 200 and 20_000_000 <= healthy["elapsed_ns"] < 5_000_000_000:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"{guest}: no healthy API response, see {log}")
                time.sleep(0.05)

            assert healthy["simulated"] is True
            assert healthy["sensor"] == {
                "id": 0, "temperature_millicelsius": 70000, "state": "normal",
            }
            assert healthy["power"] == {"enabled": True, "state": "on"}
            assert healthy["fan"]["rpm"] == 3000
            assert healthy["fan"]["duty_percent"] == 50
            assert healthy["fan"]["pwm_period_ns"] == 40_000
            assert healthy["fan"]["pwm_high"] == (healthy["elapsed_ns"] % 40_000 < 20_000)
            assert healthy["fan"]["tach_high"] == (healthy["elapsed_ns"] % 10_000_000 < 5_000_000)
            assert healthy["fan"]["protected"] is False
            assert request(port, "POST", "/v1/status") == (405, {"error": "read_only"}, "GET")
            assert request(port, "GET", "/unknown")[0] == 404

            deadline = time.monotonic() + 8
            while True:
                code, failed, _ = request(port, "GET", "/v1/status")
                assert code == 200
                if failed["fan"]["protected"]:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{guest}: fan protection did not appear, see {log}")
                time.sleep(0.05)
            assert failed["fan"] == {
                "rpm": None, "duty_percent": 100, "pwm_period_ns": 40_000,
                "pwm_high": True, "tach_high": False, "protected": True,
            }
            assert failed["sensor"]["state"] == "high"
            assert failed["power"] == healthy["power"]
            (output / f"{guest}-snapshots.json").write_text(
                json.dumps({"healthy": healthy, "tach_loss": failed}, indent=2) + "\n"
            )
            print(f"{guest}: healthy and tach-loss status verified; POST 405; unknown GET 404", flush=True)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initramfs", type=Path, required=True)
    parser.add_argument("--arceos-image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / "environment.txt").open("w") as metadata:
        metadata.write(subprocess.check_output([args.qemu, "--version"], text=True))
        for artifact in (args.kernel, args.initramfs, args.arceos_image):
            metadata.write(f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact}\n")
    for guest in ("linux", "arceos"):
        verify(args.qemu, guest, args, args.output)


if __name__ == "__main__":
    main()

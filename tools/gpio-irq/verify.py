"""Verify a PL061 GPIO3 interrupt causes the guest to assert GPIO0."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import tempfile
import time


class Qmp:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(3)
        self.socket.connect(str(path))
        self.stream = self.socket.makefile("rwb", buffering=0)
        assert "QMP" in self.receive()
        self.execute("qmp_capabilities")

    def receive(self):
        data = self.stream.readline()
        if not data:
            raise RuntimeError("QMP closed")
        return json.loads(data)

    def execute(self, name, arguments=None):
        command = {"execute": name}
        if arguments is not None:
            command["arguments"] = arguments
        self.stream.write(json.dumps(command).encode() + b"\n")
        while True:
            result = self.receive()
            if "error" in result:
                raise RuntimeError(result["error"])
            if "return" in result:
                return result["return"]

    def gpio_output(self):
        text = self.execute("human-monitor-command", {"command-line": "xp /1wx 0x09030004"})
        value = re.search(r":\s*0x([0-9a-fA-F]+)", text)
        if value is None:
            raise RuntimeError(f"cannot read GPIO0: {text}")
        return int(value[1], 16) & 1

    def close(self):
        self.stream.close()
        self.socket.close()


def boot(command, guest_name, output, boot_index, timeout_case, fan_case):
    with tempfile.TemporaryDirectory(prefix="bmc-gpio-") as temporary:
        qmp_path = Path(temporary) / "qmp"
        full_command = command + ["-qmp", f"unix:{qmp_path},server=on,wait=off"]
        log = output / f"{guest_name}-{boot_index}.log"
        qmp = None
        result = None
        injected = False
        armed = False
        cutoff = fired = disabled = None
        with log.open("wb") as logfile, subprocess.Popen(
            full_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ) as guest:
            try:
                buffer = bytearray()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    ready, _, _ = select.select([guest.stdout], [], [], remaining)
                    if not ready:
                        break
                    data = os.read(guest.stdout.fileno(), 65536)
                    if not data:
                        break
                    logfile.write(data)
                    buffer.extend(data)
                    lines = buffer.decode(errors="replace").replace("\r\n", "\n").split("\n")[:-1]
                    if timeout_case and "GPIO_ARMED" in lines and not armed:
                        qmp = Qmp(qmp_path)
                        assert qmp.gpio_output() == 1, "power output was not armed"
                        armed = True
                    for line in lines:
                        if line.startswith("GPIO_TIMEOUT,") and not injected:
                            assert timeout_case and armed
                            cutoff, fired, disabled = map(int, line.split(",")[1:])
                            assert 0 < cutoff <= fired <= disabled
                            assert qmp.gpio_output() == 0, "timer did not clear output"
                            qmp.execute("system_powerdown")
                            injected = True
                    if not timeout_case and "GPIO_READY" in lines and not injected:
                        qmp = Qmp(qmp_path)
                        assert qmp.gpio_output() == int(not fan_case), "unexpected output before injection"
                        qmp.execute("system_powerdown")
                        injected = True
                    for line in lines:
                        if line.startswith("GPIO_RESULT,"):
                            _, reported_guest, captured, finished, value = line.split(",")
                            result = (int(captured), int(finished), int(value))
                            assert reported_guest == guest_name
                    if "GPIO_DONE" in lines:
                        assert injected and result is not None
                        captured, finished, value = result
                        assert 0 < captured <= finished and value == int(not timeout_case)
                        assert qmp.gpio_output() == value, "GPIO output disagrees with guest"
                        if timeout_case:
                            assert disabled <= captured, "test did not inject after shutdown"
                        elif fan_case:
                            assert finished - captured >= 100_000_000, "fan protection triggered early"
                        else:
                            assert "GPIO_CONFIRMED" in lines, "startup did not survive its original deadline"
                        return (
                            guest_name, boot_index, captured, finished, finished - captured,
                            cutoff, fired, disabled,
                            disabled - cutoff if timeout_case else None,
                            value,
                        )
                raise RuntimeError(f"guest failed to complete; see {log}")
            finally:
                if qmp is not None:
                    qmp.close()
                if guest.poll() is None:
                    guest.terminate()
                    try:
                        guest.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        guest.kill()
                        guest.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initramfs", type=Path, required=True)
    parser.add_argument("--arceos-image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--boots", type=int, default=3)
    scenarios = parser.add_mutually_exclusive_group()
    scenarios.add_argument("--fan-case", action="store_true", help="Inject one tach edge, then stop pulses")
    scenarios.add_argument("--timeout-case", action="store_true",
                        help="Use guests built with the timeout feature and inject after output clears")
    args = parser.parse_args()
    if args.boots < 1:
        parser.error("boots must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    common = [
        args.qemu, "-M", "virt,gic-version=2", "-accel", "tcg", "-cpu", "cortex-a72",
        "-smp", "1", "-m", "256M", "-display", "none", "-serial", "stdio",
        "-monitor", "none", "-nic", "none", "-no-reboot",
    ]
    commands = {
        "linux": common + ["-kernel", str(args.kernel), "-initrd", str(args.initramfs),
                           "-append", "console=ttyAMA0 rdinit=/init quiet"],
        "arceos": common + ["-kernel", str(args.arceos_image)],
    }
    with (args.output / "environment.txt").open("w") as metadata:
        metadata.write(subprocess.check_output([args.qemu, "--version"], text=True))
        for name, command in commands.items():
            metadata.write(f"{name}: {command!r}\n")
        for file in (args.kernel, args.initramfs, args.arceos_image):
            metadata.write(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file}\n")
    rows = []
    for index in range(1, args.boots + 1):
        for name in (tuple(commands) if index % 2 else tuple(reversed(commands))):
            row = boot(commands[name], name, args.output, index, args.timeout_case, args.fan_case)
            rows.append(row)
            if args.timeout_case:
                print(f"{name} boot {index}: GPIO0 stayed low after late IRQ, "
                      f"deadline-to-clear {row[-2]} ns", flush=True)
            elif args.fan_case:
                print(f"{name} boot {index}: fan protection GPIO asserted, tach-to-protection {row[4]} ns", flush=True)
            else:
                print(f"{name} boot {index}: GPIO0 stayed high after confirmed startup, capture-to-write {row[4]} ns", flush=True)
    with (args.output / "gpio-irq.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("guest", "boot", "captured_ns", "finished_ns", "capture_to_handled_ns",
                         "deadline_ns", "timer_fired_ns", "output_cleared_ns",
                         "deadline_to_clear_ns", "output_after_irq"))
        writer.writerows(rows)


if __name__ == "__main__":
    main()

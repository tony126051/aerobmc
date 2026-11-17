"""Boot both guests, validate serial events, then summarize batch timings."""

import argparse
import csv
import hashlib
import math
import os
from pathlib import Path
import resource
import select
import statistics
import subprocess
import time


EXPECTED_EVENTS = {"stable": 1, "threshold": 100_000, "failure": 99_999}
TRIALS = 7
OPERATIONS = 100_000
PERIOD_NS = 13_000_000
PERIODIC_SAMPLES = 256
CONTROL_CYCLES = 48
CONTROL_RESULTS = {
    "fan_loss": {("protected", 0, 0)},
    "power_retry": {("on", 2, 1), ("failed", 3, 3)},
    "power_absent": {("failed", 3, 3)},
}
POWER_GOOD_EXPECTED = [
    ("level_delayed", "timeout"),
    ("edge_delayed", "on"),
    ("edge_at_deadline", "timeout"),
    ("edge_after_deadline", "timeout"),
    ("edge_level_low", "timeout"),
    ("edge_future", "timeout"),
    ("edge_after_shutdown", "ignored"),
    ("edge_old_attempt", "ignored"),
    ("edge_before_enable", "ignored"),
    ("edge_current_retry", "on"),
    ("irq_before_timer", "on"),
    ("timer_before_irq", "timeout"),
    ("deadline_irq_first", "timeout"),
    ("deadline_timer_first", "timeout"),
    ("late_irq_first", "timeout"),
    ("late_timer_first", "timeout"),
    ("early_timer", "ignored"),
    ("old_timer_after_retry", "ignored"),
    ("current_timer_after_retry", "timeout"),
]


def boot(command, log, timeout):
    deadline = time.monotonic() + timeout
    with log.open("wb") as stream, subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ) as guest:
        data = bytearray()
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([guest.stdout], [], [], deadline - time.monotonic())
                if not ready:
                    break
                chunk = os.read(guest.stdout.fileno(), 65536)
                if not chunk:
                    break
                stream.write(chunk)
                data.extend(chunk)
                if data.endswith(b"BMC_DONE\r\n") or b"\nBMC_DONE\n" in data.replace(b"\r\n", b"\n"):
                    return data.decode(errors="replace").splitlines()
            raise RuntimeError(f"guest did not complete, see {log}")
        finally:
            if guest.poll() is None:
                guest.terminate()
                try:
                    guest.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    guest.kill()


def validate(lines, expected):
    begin = lines.index("BMC_TRACE_BEGIN")
    end = lines.index("BMC_TRACE_END", begin + 1)
    done = lines.index("BMC_DONE", end + 1)
    if lines[begin + 1 : end] != expected:
        raise ValueError("serial events differ from sensor.expected.csv")

    timings = {}
    periodic = []
    control = {name: [] for name in CONTROL_RESULTS}
    outcomes = {}
    power_good = []
    for row in lines[end + 1 : done]:
        if row.startswith("BMC_POWER_GOOD,"):
            _, case, state = row.split(",")
            power_good.append((case, state))
            continue
        if row.startswith("BMC_CONTROL,"):
            _, name, index, wake, finish = row.split(",")
            index, wake, finish = map(int, (index, wake, finish))
            if name not in control or index != len(control[name]) or not 0 <= wake <= finish:
                raise ValueError(f"unexpected control cycle: {row}")
            control[name].append((index, wake, finish))
            continue
        if row.startswith("BMC_CONTROL_RESULT,"):
            _, name, state, attempts, timeouts, elapsed, lateness = row.split(",")
            attempts, timeouts, elapsed, lateness = map(int, (attempts, timeouts, elapsed, lateness))
            if (
                name in outcomes
                or (state, attempts, timeouts) not in CONTROL_RESULTS.get(name, ())
                or elapsed <= 0
                or lateness < 0
            ):
                raise ValueError(f"unexpected control result: {row}")
            outcomes[name] = (state, attempts, timeouts, elapsed, lateness)
            continue
        if row.startswith("BMC_PERIODIC,"):
            index, period, wake, event = map(int, row.split(",")[1:])
            if index != len(periodic) or period != PERIOD_NS or not 0 <= wake <= event:
                raise ValueError(f"unexpected periodic record: {row}")
            periodic.append((index, period, wake, event))
            continue
        if not row.startswith("BMC_TIMING,"):
            continue
        _, name, trial, operations, events, elapsed = row.split(",")
        trial, operations, events, elapsed = map(int, (trial, operations, events, elapsed))
        key = name, trial
        if (
            key in timings
            or name not in EXPECTED_EVENTS
            or not 1 <= trial <= TRIALS
            or operations != OPERATIONS
            or events != EXPECTED_EVENTS[name]
            or elapsed <= 0
        ):
            raise ValueError(f"unexpected timing record: {row}")
        timings[key] = elapsed
    if len(timings) != len(EXPECTED_EVENTS) * TRIALS:
        raise ValueError("missing timing records")
    if len(periodic) != PERIODIC_SAMPLES:
        raise ValueError("missing periodic timing records")
    if any(len(samples) != CONTROL_CYCLES for samples in control.values()) or outcomes.keys() != control.keys():
        raise ValueError("missing control records")
    if power_good != POWER_GOOD_EXPECTED:
        raise ValueError("power-good replay outcomes differ from expected results")
    return timings, periodic, control, outcomes, power_good


def child_cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--linux-kernel", type=Path, required=True)
    parser.add_argument("--initramfs", type=Path, required=True)
    parser.add_argument("--arceos-image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--boots", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if args.boots < 1 or args.timeout < 1:
        parser.error("boots and timeout must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    traces = Path(__file__).resolve().parents[2] / "traces"
    expected = (traces / "sensor.expected.csv").read_text().splitlines()
    common = [
        args.qemu, "-machine", "virt,gic-version=2", "-accel", "tcg",
        "-cpu", "cortex-a72", "-smp", "1", "-m", "256M",
        "-display", "none", "-serial", "stdio", "-monitor", "none", "-no-reboot",
        "-nic", "none",
    ]
    commands = {
        "linux": common + [
            "-kernel", str(args.linux_kernel), "-initrd", str(args.initramfs),
            "-append", "console=ttyAMA0 rdinit=/init quiet",
        ],
        "arceos": common + ["-kernel", str(args.arceos_image)],
    }
    qemu_version = subprocess.check_output([args.qemu, "--version"], text=True)
    with (args.output / "environment.txt").open("w") as metadata:
        metadata.write(qemu_version)
        for name, command in commands.items():
            metadata.write(f"{name}: {command!r}\n")
        for file in (args.linux_kernel, args.initramfs, args.arceos_image, traces / "sensor.csv"):
            metadata.write(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file}\n")

    rows = []
    periodic_rows = []
    control_rows = []
    outcome_rows = []
    cpu_rows = []
    power_good_rows = []
    for index in range(1, args.boots + 1):
        order = tuple(commands) if index % 2 else tuple(reversed(commands))
        for name in order:
            log = args.output / f"{name}-{index}.log"
            cpu_before = child_cpu_seconds()
            wall_before = time.monotonic()
            lines = boot(commands[name], log, args.timeout)
            cpu_rows.append((name, index, child_cpu_seconds() - cpu_before, time.monotonic() - wall_before))
            try:
                timings, periodic, control, outcomes, power_good = validate(lines, expected)
            except ValueError as error:
                raise RuntimeError(f"{log}: {error}") from error
            print(f"{name} boot {index}: {len(expected) - 1} events and {len(power_good)} power-good cases match", flush=True)
            rows.extend((name, index, workload, trial, ns)
                        for (workload, trial), ns in timings.items())
            periodic_rows.extend((name, index, *sample) for sample in periodic)
            control_rows.extend((name, index, case, *sample)
                                for case, samples in control.items() for sample in samples)
            outcome_rows.extend((name, index, case, *outcome) for case, outcome in outcomes.items())
            power_good_rows.extend((name, index, *case) for case in power_good)

    with (args.output / "timings.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("guest", "boot", "workload", "trial", "elapsed_ns"))
        writer.writerows(rows)
    with (args.output / "periodic.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("guest", "boot", "sample", "period_ns", "wake_lateness_ns", "event_lateness_ns"))
        writer.writerows(periodic_rows)
    for filename, fields, records in (
        ("control.csv", ("guest", "boot", "scenario", "cycle", "wake_lateness_ns", "finish_lateness_ns"), control_rows),
        ("control-outcomes.csv", ("guest", "boot", "scenario", "state", "attempts", "timeouts",
                                  "response_ns", "worst_timeout_lateness_ns"), outcome_rows),
        ("qemu-cpu.csv", ("guest", "boot", "host_cpu_seconds", "host_wall_seconds"), cpu_rows),
        ("power-good.csv", ("guest", "boot", "case", "outcome"), power_good_rows),
    ):
        with (args.output / filename).open("w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(fields)
            writer.writerows(records)
    for name in commands:
        for workload in EXPECTED_EVENTS:
            elapsed = [ns for guest, _, case, _, ns in rows if guest == name and case == workload]
            print(f"{name} {workload}: median {statistics.median(elapsed) / 1e6:.3f} ms "
                  f"(range {min(elapsed) / 1e6:.3f}–{max(elapsed) / 1e6:.3f} ms)")
        samples = [(wake, event) for guest, _, _, _, wake, event in periodic_rows if guest == name]
        for metric, values in (
            ("wake", [wake for wake, _ in samples]),
            ("event", [event for _, event in samples]),
            ("processing", [event - wake for wake, event in samples]),
        ):
            p99 = sorted(values)[math.ceil(0.99 * len(values)) - 1]
            print(f"{name} periodic {metric}: median {statistics.median(values) / 1e3:.3f} us, "
                  f"p99 {p99 / 1e3:.3f} us, max {max(values) / 1e3:.3f} us")
        misses = sum(event >= PERIOD_NS for _, event in samples)
        print(f"{name} periodic: {misses}/{len(samples)} events reached the next release deadline")


if __name__ == "__main__":
    main()

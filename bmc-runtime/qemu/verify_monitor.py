"""Verify the SQLite monitor against Linux and ArceOS Runtime images."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from verify import run


def verify(qemu, kernel, images, output, guest):
    archive = output / f"{guest}.sqlite"
    reports = {}

    def collect(port):
        return subprocess.run(
            [sys.executable, str(Path(__file__).with_name("monitor.py")),
             f"http://127.0.0.1:{port}", str(archive), "--source", guest, "--once"],
            capture_output=True, text=True,
        )

    def poll(port):
        result = collect(port)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def workload(vm, fixture, request, wait_for, port):
        def serial(*commands):
            vm.stdin.write(("\n".join(commands) + "\n").encode())
            vm.stdin.flush()

        serial("temperature 1 85000")
        wait_for(lambda status: status["sensors"][1]["critical"])
        reports["first"] = poll(port)
        assert reports["first"]["archived"] == 2
        assert poll(port)["archived"] == 0

        serial("temperature 1 55000", "start")
        wait_for(lambda status: status["power"]["state"] == "starting")
        fixture.level(5, True)
        wait_for(lambda status: status["power"]["state"] == "on")
        assert fixture.outputs() & 1
        serial("temperature 1 85000")
        wait_for(lambda status: status["power"]["state"] == "thermal_tripped")
        assert not fixture.outputs() & 1
        reports["resume"] = poll(port)
        assert reports["resume"]["alerts"] == [] and reports["resume"]["archived"] >= 5
        assert poll(port)["archived"] == 0

        # Leave the collector stopped while generating more events than the guest retains.
        for _ in range(12):
            serial("temperature 1 55000")
            wait_for(lambda status: not status["sensors"][1]["critical"])
            serial("temperature 1 85000")
            wait_for(lambda status: status["sensors"][1]["critical"])
        reports["overrun"] = poll(port)
        assert reports["overrun"]["archived"] == 32
        assert reports["overrun"]["alerts"] == [{
            "kind": "events_missing", "first": reports["resume"]["sequence"] + 1,
            "last": reports["overrun"]["sequence"] - 32,
        }]
        assert poll(port)["alerts"] == []
        reports["port"] = port

    def rebooted(vm, fixture, request, wait_for, port):
        reports["reboot"] = poll(port)
        assert reports["reboot"]["generation"] == 2
        assert reports["reboot"]["alerts"][0]["kind"] == "stream_reset"
        vm.stdin.write(b"temperature 1 85000\n")
        vm.stdin.flush()
        wait_for(lambda status: status["sensors"][1]["critical"])
        assert poll(port)["archived"] == 2

    for label, scenario in (("original", workload), ("reboot", rebooted)):
        directory = output / f"{guest}-{label}"
        directory.mkdir()
        with tempfile.TemporaryDirectory(prefix="bmc-mon-", dir="/tmp") as sockets:
            run(qemu, kernel, directory, guest, Path(sockets), images=images, workload=scenario)
        if label == "original":
            failure = collect(reports.pop("port"))
            assert failure.returncode == 1 and not failure.stdout
    with sqlite3.connect(archive) as db:
        events = db.execute("SELECT generation, sequence, payload FROM events ORDER BY generation, sequence").fetchall()
        assert any(json.loads(payload)["kind"] == "thermal_tripped" for _, _, payload in events)
        assert [(g, seq) for g, seq, _ in events if g == 2] == [(2, 1), (2, 2)]
        assert len(events) == reports["first"]["archived"] + reports["resume"]["archived"] + 34
    (output / f"{guest}-monitor.json").write_text(json.dumps(reports, indent=2) + "\n")
    print(f"{guest}: persistent archive, reconnect, overwrite gap and guest reboot verified", flush=True)


if __name__ == "__main__":
    qemu, kernel, images, output = sys.argv[1:]
    output = Path(output)
    output.mkdir()
    for guest in ("linux", "arceos"):
        verify(qemu, kernel, Path(images), output, guest)

"""Drive the new runtime through serial input and inspect its HTTP snapshots."""

from http.client import HTTPConnection, HTTPException
from contextlib import closing
import json
from pathlib import Path
import socket
import secrets
import subprocess
import sys
import tempfile
import time


class Fixture:
    PERIOD_NS = 40_000

    def __init__(self, directory):
        self.qtest = self.connect(directory / "test")
        with self.connect(directory / "monitor") as qmp:
            assert "QMP" in json.loads(qmp.readline())
            self.qmp(qmp, "qmp_capabilities")
            entries = self.qmp(qmp, "qom-list", {"path": "/machine/unattached"})
        devices = [entry["name"] for entry in entries if entry["type"] == "child<pl061>"]
        assert len(devices) == 1, devices
        self.gpio = "/machine/unattached/" + devices[0]

    @staticmethod
    def connect(path):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(2)
            sock.connect(str(path))
            return sock.makefile("rwb")

    @staticmethod
    def qmp(stream, command, args=None):
        message = {"execute": command}
        if args:
            message["arguments"] = args
        stream.write(json.dumps(message).encode() + b"\n")
        stream.flush()
        while line := stream.readline():
            value = json.loads(line)
            if "error" in value:
                raise RuntimeError(value["error"])
            if "return" in value:
                return value["return"]
        raise RuntimeError("QMP closed")

    def command(self, command):
        self.qtest.write(command.encode() + b"\n")
        self.qtest.flush()
        reply = self.qtest.readline().decode().strip().split()
        assert reply and reply[0] == "OK", (command, reply)
        return reply[1:]

    def level(self, pin, high):
        self.command(f"set_irq_in {self.gpio} unnamed-gpio-in {pin} {int(high)}")

    def outputs(self):
        return int(self.command("readl 0x0903001c")[0], 16) & 7

    def pwm(self):
        outputs = self.outputs()
        duty = 100 if outputs & 2 else 75 if outputs & 4 else 50
        # Ideal 25 kHz software fixture. Sample both sides of the duty boundary.
        high_ns = self.PERIOD_NS * duty // 100
        return {"period_ns": self.PERIOD_NS, "duty_percent": duty,
                "levels": [phase % self.PERIOD_NS < high_ns for phase in (0, 19999, 20000, 39999, 40000)]}

    def tach(self, count):
        duty = self.pwm()["duty_percent"]
        motor_rpm = 6000 * duty // 100
        period = 60 / (motor_rpm * 2)
        target = time.monotonic()
        for _ in range(count):
            time.sleep(max(0, target - time.monotonic()))
            self.level(4, True)
            self.level(4, False)
            target += period

    def close(self):
        self.qtest.close()


def guest_command(qemu, kernel, images, guest, port):
    command = [
        qemu, "-M", "virt,gic-version=2", "-accel", "tcg", "-cpu", "cortex-a72",
        "-smp", "1", "-m", "256M", "-display", "none", "-monitor", "none",
        "-serial", "stdio", "-no-reboot", "-global", "virtio-mmio.force-legacy=false",
        "-netdev", f"user,id=n,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:5555",
        "-device", "virtio-net-device,netdev=n",
    ]
    if guest == "linux":
        command += ["-kernel", kernel, "-initrd", str(images / "linux.cpio"),
                    "-append", "console=ttyAMA0 rdinit=/init quiet"]
    else:
        command += ["-kernel", str(images / "arceos.bin")]

    return command


def run(qemu, kernel, output, guest, directory, retry_success=False, *, images=None, workload=None, port=None):
    artifact = f"{guest}-retry-success" if retry_success else guest
    images = output if images is None else images
    if port is None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    command = guest_command(qemu, kernel, images, guest, port)
    command += [
        "-qmp", f"unix:{directory / 'monitor'},server=on,wait=off",
        "-qtest", f"unix:{directory / 'test'},server=on,wait=off", "-qtest-log", "/dev/null",
    ]

    log = output / f"{artifact}.log"
    (output / f"{artifact}-command.json").write_text(json.dumps(command, indent=2) + "\n")

    def request(method="GET", path="/v1/status", headers=None, body=None, *, session=None, with_headers=False):
        conn = HTTPConnection("127.0.0.1", port, timeout=0.5) if session is None else session
        try:
            conn.request(method, path, body=body, headers=headers or {})
            reply = conn.getresponse()
            payload = reply.read()
            result = (reply.status, json.loads(payload) if payload else None)
            return (*result, reply.headers) if with_headers else result
        finally:
            if session is None:
                conn.close()

    with log.open("wb") as serial, subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=serial, stderr=subprocess.STDOUT,
    ) as vm:
        actions = []

        def submit(name, key=None):
            headers = {"X-BMC-Action": "1"}
            if key is not None:
                headers["Idempotency-Key"] = key
            code, reply = request("POST", f"/v1/actions/{name}", headers)
            assert code == 202 and reply["queued"] is True
            assert reply["id"] == len(actions) + 1
            actions.append((name, reply["id"]))
            return reply["id"]

        def wait_for(condition, seconds=3):
            end = time.monotonic() + seconds
            while vm.poll() is None and time.monotonic() < end:
                try:
                    code, value = request()
                    if code == 200 and condition(value):
                        return value
                except (OSError, HTTPException):
                    pass
                time.sleep(0.02)
            raise RuntimeError(f"{guest} did not reach the expected state, see {log}")

        def inject(*commands):
            vm.stdin.write(("\n".join(commands) + "\n").encode())
            vm.stdin.flush()

        fixture = None
        try:
            initial = wait_for(lambda value: value["power"]["state"] == "off", seconds=20)
            assert request(path="/v1/events") == (
                200, {"revision": initial["revision"], "overwritten": 0, "events": []},
            )
            fixture = Fixture(directory)
            assert fixture.outputs() == 0
            if workload is not None:
                workload(vm, fixture, request, wait_for, port)
                return
            if retry_success:
                with closing(HTTPConnection("127.0.0.1", port, timeout=0.5)) as session:
                    for _ in range(3):
                        assert request(headers={"Connection": "keep-alive"}, session=session) == (200, initial)
                        assert session.sock is not None
                        connection = session.sock if _ == 0 else connection
                        assert session.sock is connection
                    assert request() == (200, initial)
                    assert request(session=session) == (200, initial)
                    assert session.sock is None
                path = "/v1/actions/start"
                header = {"X-BMC-Action": "1"}
                assert request("GET", path)[0] == 404
                assert request("POST", path)[0] == 403
                assert request("POST", path, header, b"x")[0] == 400
                assert request("POST", "/v1/actions/power-good", header)[0] == 404
                assert request()[1] == initial and fixture.outputs() == 0
                submit("start")
                wait_for(lambda value: value["power_attempt"] == 1
                         and value["power"]["state"] == "retry_wait")
                assert fixture.outputs() & 1 == 0
                wait_for(lambda value: value["power_attempt"] == 2
                         and value["power"]["state"] == "starting")
                assert fixture.outputs() & 1 == 1
                fixture.level(5, True)
                confirmed = wait_for(lambda value: value["power"]["state"] == "on")
                time.sleep(0.25)
                code, still_on = request()
                assert code == 200 and still_on["power"]["state"] == "on"
                assert confirmed["power_attempt"] == still_on["power_attempt"] == 2
                assert fixture.outputs() & 1 == 1
                (output / f"{artifact}.json").write_text(json.dumps(still_on, indent=2) + "\n")
                print(f"{guest}: second-attempt power-good prevented further retries", flush=True)
                assert still_on["watchdog"]["armed"] is True
                assert still_on["watchdog"]["timeout_ms"] == 500
                for pulse in range(1, 5):
                    fixture.level(6, True)
                    fixture.level(6, False)
                    heartbeat = wait_for(lambda value: value["watchdog"]["heartbeat_edges"] == pulse)
                    assert heartbeat["power"]["state"] == "on"
                    assert fixture.outputs() & 1 == 1
                    if pulse != 4:
                        time.sleep(0.18)
                time.sleep(0.55)  # Let the watchdog expire without HTTP requests.
                tripped = wait_for(lambda value: value["power"]["state"] == "watchdog_tripped")
                assert tripped["watchdog"] == {
                    "armed": False, "heartbeat_edges": 4, "trips": 1, "timeout_ms": 500,
                }
                assert tripped["power"]["enabled"] is False
                assert fixture.outputs() & 1 == 0
                fixture.level(6, True)
                fixture.level(6, False)
                late = wait_for(lambda value: value["watchdog"]["heartbeat_edges"] == 5)
                assert late["power"] == tripped["power"]
                time.sleep(0.23)  # Cross two normal retry intervals.
                assert fixture.outputs() & 1 == 0
                assert request()[1]["power_attempt"] == 2
                code, journal = request(path="/v1/events")
                assert code == 200
                trips = [event for event in journal["events"] if event["kind"] == "watchdog_tripped"]
                assert len(trips) == 1 and trips[0]["revision"] == tripped["revision"]
                fixture.level(5, False)
                inject("start")
                wait_for(lambda value: value["power"]["state"] == "starting")
                fixture.level(5, True)
                restarted = wait_for(lambda value: value["power"]["state"] == "on")
                assert restarted["power_attempt"] == 1
                assert restarted["watchdog"]["armed"] is True
                assert restarted["watchdog"]["trips"] == 1
                assert fixture.outputs() & 1 == 1
                report = dict(heartbeat=heartbeat, tripped=tripped, late=late, restarted=restarted, event=trips[0])
                (output / f"{guest}-watchdog.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: GPIO heartbeat, watchdog shutdown, late pulse and manual restart verified", flush=True)
                fixture.level(5, False)
                time.sleep(0.015)
                fixture.level(5, True)
                glitch = wait_for(lambda value: value["power_good_edges"] > restarted["power_good_edges"])
                assert glitch["power"]["state"] == "on"
                assert fixture.outputs() & 1 == 1

                fixture.level(6, True)
                fixture.level(6, False)
                alive = wait_for(lambda value: value["watchdog"]["heartbeat_edges"] == 6)
                assert alive["power"]["state"] == "on"
                fixture.level(5, False)
                time.sleep(0.09)  # Leave the API idle during power loss.
                assert fixture.outputs() & 1 == 0
                lost = wait_for(lambda value: value["power"]["state"] == "power_good_lost")
                assert lost["power"]["enabled"] is False
                assert lost["watchdog"]["armed"] is False
                assert lost["watchdog"]["trips"] == 1
                code, journal = request(path="/v1/events")
                assert code == 200
                faults = [event for event in journal["events"] if event["kind"] == "power_good_lost"]
                assert len(faults) == 1 and faults[0]["revision"] == lost["revision"]

                fixture.level(5, True)
                late_good = wait_for(lambda value: value["power_good_edges"] > lost["power_good_edges"])
                time.sleep(0.23)
                assert request()[1]["power"] == late_good["power"] == lost["power"]
                assert fixture.outputs() & 1 == 0
                inject("start")
                starting = wait_for(lambda value: value["power"]["state"] == "starting")
                assert starting["power_good_edges"] == late_good["power_good_edges"]
                fixture.level(5, False)
                fixture.level(5, True)
                restored = wait_for(lambda value: value["power"]["state"] == "on")
                assert restored["power_attempt"] == 1 and restored["watchdog"]["armed"]
                assert fixture.outputs() & 1 == 1
                report = dict(glitch=glitch, alive=alive, lost=lost, late_good=late_good,
                              restored=restored, event=faults[0])
                (output / f"{guest}-power-good-loss.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: short power-good glitch, sustained loss and explicit restart verified", flush=True)

                submit("stop")
                submit("stop")
                stopped = wait_for(lambda value: value["power"]["state"] == "off")
                assert stopped["power_attempt"] == 0 and not stopped["watchdog"]["armed"]
                assert fixture.outputs() & 1 == 0
                fixture.level(5, False)
                fixture.level(5, True)
                fixture.level(6, True)
                fixture.level(6, False)
                late = wait_for(lambda value: value["power_good_edges"] > stopped["power_good_edges"]
                                and value["watchdog"]["heartbeat_edges"] > stopped["watchdog"]["heartbeat_edges"])
                time.sleep(0.55)
                assert request()[1]["power"] == late["power"] == stopped["power"]
                assert request()[1]["watchdog"]["trips"] == stopped["watchdog"]["trips"]
                assert fixture.outputs() & 1 == 0

                inject("start")
                retry_wait = wait_for(lambda value: value["power"]["state"] == "retry_wait")
                assert retry_wait["power_attempt"] == 1
                inject("stop")
                cancelled = wait_for(lambda value: value["power"]["state"] == "off")
                time.sleep(0.23)
                assert request()[1]["power"] == cancelled["power"]
                assert cancelled["power_attempt"] == 0 and fixture.outputs() & 1 == 0
                code, journal = request(path="/v1/events")
                assert code == 200 and journal["overwritten"] == 0
                stops = [event for event in journal["events"] if event["kind"] == "power_stopped"]
                assert [event["revision"] for event in stops] == [stopped["revision"], cancelled["revision"]]
                assert all(event["kind"] != "power_retry_started" for event in journal["events"]
                           if event["revision"] > retry_wait["revision"])

                inject("start")
                starting = wait_for(lambda value: value["power"]["state"] == "starting")
                assert starting["power_good_edges"] == late["power_good_edges"]
                fixture.level(5, False)
                fixture.level(5, True)
                restarted = wait_for(lambda value: value["power"]["state"] == "on")
                assert restarted["power_attempt"] == 1 and restarted["watchdog"]["armed"]
                assert fixture.outputs() & 1 == 1
                report = dict(stopped=stopped, late=late, retry_wait=retry_wait, cancelled=cancelled,
                              restarted=restarted, events=stops)
                (output / f"{guest}-power-stop.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: manual stop, retry cancellation and fresh start verified", flush=True)
                cycle_id = submit("cycle", "test-cycle")
                inject("cycle", "start")
                cycling = wait_for(lambda value: value["power"]["state"] == "cycle_wait")
                assert cycling["power_attempt"] == 0 and not cycling["watchdog"]["armed"]
                assert fixture.outputs() & 1 == 0
                fixture.level(5, False)
                fixture.level(5, True)
                early = wait_for(lambda value: value["power_good_edges"] > cycling["power_good_edges"])
                assert early["power"]["state"] == "cycle_wait" and fixture.outputs() & 1 == 0
                starting = wait_for(lambda value: value["power"]["state"] == "starting")
                assert starting["power_attempt"] == 1 and fixture.outputs() & 1 == 1
                assert starting["power_good_edges"] == early["power_good_edges"]
                fixture.level(5, False)
                fixture.level(5, True)
                cycled = wait_for(lambda value: value["power"]["state"] == "on")
                assert cycled["watchdog"]["armed"] and cycled["power_attempt"] == 1
                replay_headers = {"X-BMC-Action": "1", "Idempotency-Key": "test-cycle"}
                replay = request("POST", "/v1/actions/cycle", replay_headers)
                conflict = request("POST", "/v1/actions/stop", replay_headers)
                assert replay == (202, {"queued": True, "id": cycle_id})
                assert conflict == (409, {"error": "idempotency_key_conflict"})

                inject("cycle")
                pending_cycle = wait_for(lambda value: value["power"]["state"] == "cycle_wait")
                assert fixture.outputs() & 1 == 0
                inject("stop")
                cancelled_cycle = wait_for(lambda value: value["power"]["state"] == "off")
                time.sleep(0.23)
                assert request()[1]["power"] == cancelled_cycle["power"] and fixture.outputs() & 1 == 0
                code, journal = request(path="/v1/events")
                assert code == 200
                cycle_events = [event for event in journal["events"]
                                if event["revision"] > restarted["revision"]]
                assert [event["kind"] for event in cycle_events] == [
                    "power_cycle_scheduled", "power_good_ignored", "power_cycle_started",
                    "power_on", "power_cycle_scheduled", "power_stopped",
                ]
                assert cycle_events[2]["at_ms"] - cycle_events[0]["at_ms"] >= 100
                report = dict(cycling=cycling, early=early, starting=starting, cycled=cycled,
                              pending_cycle=pending_cycle, cancelled=cancelled_cycle, events=cycle_events)
                (output / f"{guest}-power-cycle.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: power-cycle dwell, fresh feedback and cancellation verified", flush=True)
                assert cancelled_cycle["fan"]["protected"] and cancelled_cycle["fan"]["rpm"] is None
                submit("fan-reset")
                rejected = wait_for(lambda value: value["revision"] > cancelled_cycle["revision"])
                assert rejected["fan"]["protected"] and fixture.pwm()["duty_percent"] == 100
                inject("temperature 1 68000")
                hot = wait_for(lambda value: value["sensors"][1]["state"] == "high")
                fixture.tach(40)
                stable = wait_for(lambda value: value["fan"]["rpm"] is not None)
                assert stable["fan"]["protected"] and fixture.pwm()["duty_percent"] == 100
                submit("fan-reset")
                recovered = wait_for(lambda value: not value["fan"]["protected"])
                assert recovered["fan"]["duty_percent"] == fixture.pwm()["duty_percent"] == 75
                inject("temperature 1 60000")
                cool = wait_for(lambda value: value["fan"]["duty_percent"] == 50)
                assert fixture.pwm()["duty_percent"] == 50
                relapsed = wait_for(lambda value: value["fan"]["protected"])
                assert relapsed["fan"]["rpm"] is None and fixture.pwm()["duty_percent"] == 100
                code, journal = request(path="/v1/events")
                assert code == 200
                fan_events = [event for event in journal["events"]
                              if event["revision"] > cancelled_cycle["revision"]
                              and event["kind"].startswith("fan_")]
                assert [(event["kind"], event["revision"]) for event in fan_events] == [
                    ("fan_reset_rejected", rejected["revision"]),
                    ("fan_recovered", recovered["revision"]),
                    ("fan_protection", relapsed["revision"]),
                ]
                report = dict(rejected=rejected, hot=hot, stable=stable, recovered=recovered,
                              cool=cool, relapsed=relapsed, events=fan_events)
                (output / f"{guest}-fan-recovery.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: fan reset gating, thermal handoff and repeated loss verified", flush=True)
                fixture.level(5, False)
                inject("start")
                wait_for(lambda value: value["power"]["state"] == "starting")
                fixture.level(5, True)
                running = wait_for(lambda value: value["power"]["state"] == "on")
                inject("temperature 1 85000", "temperature 0 95000")
                tripped = wait_for(lambda value: value["power"]["state"] == "thermal_tripped"
                                   and all(sensor["critical"] for sensor in value["sensors"]))
                assert fixture.outputs() & 1 == 0 and fixture.pwm()["duty_percent"] == 100
                assert not tripped["watchdog"]["armed"]
                expired = wait_for(lambda value: all(sensor["state"] == "stale" for sensor in value["sensors"]))
                assert all(sensor["critical"] for sensor in expired["sensors"])
                inject("temperature 1 75000", "start", "temperature 0 85001")
                blocked = wait_for(lambda value: value["sensors"][0]["temperature_millicelsius"] == 85001)
                assert blocked["power"]["state"] == "thermal_tripped"
                assert [sensor["critical"] for sensor in blocked["sensors"]] == [True, False]
                assert fixture.outputs() & 1 == 0
                inject("temperature 0 85000")
                cooled = wait_for(lambda value: not any(sensor["critical"] for sensor in value["sensors"]))
                assert cooled["power"]["state"] == "thermal_tripped" and fixture.outputs() & 1 == 0
                fixture.level(5, False)
                inject("start")
                wait_for(lambda value: value["power"]["state"] == "starting")
                fixture.level(5, True)
                restored = wait_for(lambda value: value["power"]["state"] == "on")
                assert restored["watchdog"]["armed"] and fixture.outputs() & 1 == 1
                code, journal = request(path="/v1/events")
                assert code == 200
                thermal_events = [event for event in journal["events"] if event["revision"] > running["revision"]
                                  and ("critical" in event["kind"] or event["kind"].startswith("thermal_"))]
                assert [(event["kind"], event["sensor_id"]) for event in thermal_events] == [
                    ("sensor_critical", 1), ("thermal_tripped", 1), ("sensor_critical", 0),
                    ("sensor_critical_cleared", 1), ("thermal_start_blocked", None),
                    ("sensor_critical_cleared", 0),
                ]
                report = dict(tripped=tripped, expired=expired, blocked=blocked, cooled=cooled,
                              restored=restored, events=thermal_events)
                (output / f"{guest}-thermal-trip.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: critical thermal shutdown, stale interlock and explicit restart verified", flush=True)
                # Refresh only one sensor so the first expiry is observable.
                time.sleep(0.25)
                inject("temperature 1 55000")
                wait_for(lambda value: value["sensors"][1]["temperature_millicelsius"] == 55000)
                partial = None
                for _ in range(16):
                    fixture.level(6, True)
                    fixture.level(6, False)
                    code, value = request()
                    assert code == 200
                    if value["power"]["state"] == "thermal_telemetry_lost":
                        break
                    assert value["power"]["state"] == "on"
                    if value["sensors"][0]["state"] == "stale":
                        partial = value
                        assert value["sensors"][1]["state"] == "normal"
                    time.sleep(0.1)
                lost = value
                assert partial is not None
                assert lost["power"]["state"] == "thermal_telemetry_lost" and fixture.outputs() & 1 == 0
                assert not lost["watchdog"]["armed"] and lost["watchdog"]["trips"] == restored["watchdog"]["trips"]
                inject("start")
                blocked = wait_for(lambda value: value["revision"] > lost["revision"])
                assert blocked["power"] == lost["power"]
                inject("temperature 1 55000")
                fresh = wait_for(lambda value: value["sensors"][1]["state"] == "normal")
                assert fresh["power"] == lost["power"] and fixture.outputs() & 1 == 0
                fixture.level(5, False)
                inject("start")
                wait_for(lambda value: value["power"]["state"] == "starting")
                fixture.level(5, True)
                recovered = wait_for(lambda value: value["power"]["state"] == "on")
                assert fixture.outputs() & 1 == 1 and recovered["watchdog"]["armed"]
                code, journal = request(path="/v1/events")
                assert code == 200
                events = [event for event in journal["events"] if event["revision"] > restored["revision"]
                          and event["kind"] != "sensor_cleared"]
                assert [event["kind"] for event in events] == [
                    "sensor_stale", "sensor_stale", "thermal_telemetry_lost",
                    "thermal_start_blocked", "sensor_recovered", "power_started", "power_on",
                ], events
                report = dict(partial=partial, lost=lost, blocked=blocked, fresh=fresh,
                              recovered=recovered, events=events)
                (output / f"{guest}-thermal-telemetry.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: partial and total temperature telemetry loss and restart verified", flush=True)
                receipts = []
                expected = [
                    ("applied", "power_started"), ("applied", "power_stopped"), ("no_op", None),
                    ("applied", "power_cycle_scheduled"), ("rejected", "fan_reset_rejected"),
                    ("applied", "fan_recovered"),
                ]
                assert len(actions) == len(expected)
                for (name, action_id), (state, event) in zip(actions, expected):
                    code, receipt = request(path=f"/v1/actions/{action_id}")
                    assert code == 200
                    assert (receipt["id"], receipt["state"], receipt["event"]) == (action_id, state, event)
                    assert 0 < receipt["revision"] <= recovered["revision"]
                    receipts.append(dict(action=name, **receipt))
                assert request(path="/v1/actions/999999")[0] == 404
                (output / f"{guest}-action-receipts.json").write_text(json.dumps(receipts, indent=2) + "\n")
                (output / f"{guest}-action-replay.json").write_text(json.dumps(
                    dict(original_id=cycle_id, replay=replay, conflict=conflict, cycle_events=cycle_events),
                    indent=2,
                ) + "\n")
                def resource(path):
                    code, value = request(path=path)
                    assert code == 200
                    return value

                root = resource("/redfish/v1/")
                system_path = resource(root["Systems"]["@odata.id"])["Members"][0]["@odata.id"]
                system = resource(system_path)
                assert system["PowerState"] == "On"
                chassis = resource(resource(root["Chassis"]["@odata.id"])["Members"][0]["@odata.id"])
                sensors = resource(chassis["Sensors"]["@odata.id"])
                readings = [resource(member["@odata.id"]) for member in sensors["Members"]]
                assert [sensor["ReadingUnits"] for sensor in readings] == ["Cel", "Cel"]
                assert readings[1]["Reading"] == 55
                reset = system["Actions"]["#ComputerSystem.Reset"]["target"]
                headers = {"X-BMC-Action": "1", "Content-Type": "application/json"}
                assert request("POST", reset, headers, b'{"ResetType":"Nmi"}')[0] == 400

                def reset_host(kind, key):
                    body = json.dumps({"ResetType": kind}).encode()
                    result = request("POST", reset, dict(headers, **{"Idempotency-Key": key}),
                                     body, with_headers=True)
                    assert result[0] == 202
                    return result[1]["@odata.id"], result[2]["Location"]

                cycle_task, monitor = reset_host("PowerCycle", "rf-cycle")
                wait_for(lambda value: value["power"]["state"] == "cycle_wait")
                assert fixture.outputs() & 1 == 0
                fixture.level(5, False)
                wait_for(lambda value: value["power"]["state"] == "starting")
                fixture.level(5, True)
                wait_for(lambda value: value["power"]["state"] == "on")
                assert reset_host("PowerCycle", "rf-cycle") == (cycle_task, monitor)
                assert resource(cycle_task)["TaskState"] == "Completed"
                assert request(path=monitor)[0] == 204
                reset_host("ForceOff", "rf-stop")
                wait_for(lambda value: value["power"]["state"] == "off")
                assert fixture.outputs() & 1 == 0 and resource(system_path)["PowerState"] == "Off"
                fixture.level(5, False)
                inject("temperature 1 55000")
                reset_host("On", "rf-start")
                wait_for(lambda value: value["power"]["state"] == "starting")
                fixture.level(5, True)
                wait_for(lambda value: value["power"]["state"] == "on")
                assert fixture.outputs() & 1 == 1 and resource(system_path)["PowerState"] == "On"
                report = dict(root=root, system=system, chassis=chassis, sensors=readings,
                              cycle_task=resource(cycle_task), monitor=monitor, final_power="On")
                (output / f"{guest}-redfish.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: Redfish discovery, sensors, reset, task and GPIO verified", flush=True)
                logs = resource(system["LogServices"]["@odata.id"])
                log_path = logs["Members"][0]["@odata.id"]
                log = resource(log_path)
                assert log["MaxNumberOfRecords"] == 32 and log["OverWritePolicy"] == "WrapsWhenFull"
                entries_path = log["Entries"]["@odata.id"]
                inject("temperature 1 85000")
                wait_for(lambda value: value["power"]["state"] == "thermal_tripped")
                assert fixture.outputs() & 1 == 0
                fault = resource(system_path)
                assert fault["PowerState"] == "Off" and fault["Status"]["Health"] == "Critical"
                assert fault["Oem"]["BmcG1"]["PowerFault"] == "thermal_tripped"
                entries = resource(entries_path)
                ids = [int(member["@odata.id"].rsplit("/", 1)[1]) for member in entries["Members"]]
                assert ids == list(range(ids[0], ids[0] + 32)) and ids[0] > 1
                assert request(path=f"{entries_path}/1")[0] == 404
                diagnosis = resource(f"{entries_path}/{ids[-1]}")
                assert diagnosis["Message"] == "thermal_tripped" and diagnosis["Severity"] == "Critical"
                assert diagnosis["Oem"]["BmcG1"]["Revision"] == fault["Oem"]["BmcG1"]["Revision"]
                origin = diagnosis["Links"]["OriginOfCondition"]["@odata.id"]
                assert resource(origin)["Status"]["Health"] == "Critical"
                rejected_task, rejected_monitor = reset_host("On", "rf-thermal-block")
                wait_for(lambda value: value["revision"] > fault["Oem"]["BmcG1"]["Revision"])
                assert request(path=rejected_monitor)[0] == 409
                assert resource(rejected_task)["TaskState"] == "Exception"
                assert fixture.outputs() & 1 == 0
                fixture.level(5, False)
                inject("temperature 0 42000", "temperature 1 55000")
                wait_for(lambda value: value["sensors"][0]["temperature_millicelsius"] == 42000
                         and value["sensors"][1]["temperature_millicelsius"] == 55000)
                recovery_task, recovery_monitor = reset_host("On", "rf-thermal-recover")
                wait_for(lambda value: value["power"]["state"] == "starting")
                fixture.level(5, True)
                wait_for(lambda value: value["power"]["state"] == "on")
                restored_system = resource(system_path)
                assert restored_system["PowerState"] == "On" and fixture.outputs() & 1 == 1
                assert restored_system["Oem"]["BmcG1"]["PowerFault"] is None
                assert request(path=recovery_monitor)[0] == 204
                report = dict(fault=fault, diagnosis=diagnosis, log=resource(log_path),
                              rejected=resource(rejected_task), recovery=resource(recovery_task),
                              restored_system=restored_system)
                (output / f"{guest}-redfish-diagnostics.json").write_text(json.dumps(report, indent=2) + "\n")
                print(f"{guest}: Redfish fault diagnosis, log rollover and recovery verified", flush=True)
                reader, operator = secrets.token_hex(24), secrets.token_hex(24)
                read_headers = {"Authorization": f"Bearer {reader}"}
                operate_headers = {"Authorization": f"Bearer {operator}", "X-BMC-Action": "1"}
                inject(f"auth-enable {reader} {operator}")
                deadline = time.monotonic() + 2
                while request()[0] != 401:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                assert request(path=system_path)[0] == 401
                assert request(path="/v1/events", headers=read_headers)[0] == 200
                assert request(path=system_path, headers=read_headers)[0] == 200
                assert request("POST", "/v1/actions/stop", {"X-BMC-Action": "1"})[0] == 401
                assert request("POST", reset, dict(read_headers, **{"X-BMC-Action": "1"}),
                               b'{"ResetType":"ForceOff"}')[0] == 403
                assert request("POST", "/v1/actions/stop", dict(read_headers, **{"X-BMC-Action": "1"}))[0] == 403
                assert fixture.outputs() & 1
                with tempfile.TemporaryDirectory(prefix="bmc-token-") as tmp:
                    token = Path(tmp) / "reader"
                    token.touch(mode=0o600)
                    token.write_text(reader)
                    result = subprocess.run([
                        sys.executable, str(Path(__file__).with_name("monitor.py")),
                        f"http://127.0.0.1:{port}", str(output / f"{guest}-protected.sqlite"),
                        "--once", "--token-file", str(token),
                    ], capture_output=True, text=True, check=True)
                    assert json.loads(result.stdout)["archived"] > 0
                code, accepted = request("POST", "/v1/actions/stop", operate_headers)
                assert code == 202
                deadline = time.monotonic() + 2
                while fixture.outputs() & 1:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                code, receipt = request(path=f"/v1/actions/{accepted['id']}", headers=read_headers)
                assert code == 200 and receipt["state"] == "applied"
                (output / f"{guest}-access.json").write_text(json.dumps(
                    dict(anonymous=401, reader_write=403, operator_write=202, receipt=receipt),
                    indent=2,
                ) + "\n")
                print(f"{guest}: authenticated monitoring and operator-only GPIO control verified", flush=True)
                (output / f"{guest}-actions.json").write_text(json.dumps(
                    dict(accepted=[name for name, _ in actions], rejected_statuses=[404, 403, 400, 404],
                         final_power=recovered["power"]), indent=2,
                ) + "\n")
                return
            normal_pwm = fixture.pwm()
            assert normal_pwm["levels"] == [True, True, False, False, True]
            inject("temperature 0 82000", "temperature 1 55000")
            hot = wait_for(lambda value: value["sensors"][0]["state"] == "high"
                           and value["sensors"][1]["state"] == "normal")
            assert hot["fan"]["duty_percent"] == fixture.pwm()["duty_percent"] == 75
            inject("temperature 0 77000", "temperature 1 68000")
            hysteresis = wait_for(lambda value: value["sensors"][0]["temperature_millicelsius"] == 77000
                                  and value["sensors"][1]["state"] == "high")
            assert hysteresis["sensors"][0]["state"] == "high"
            inject("temperature 0 75000")
            one_hot = wait_for(lambda value: value["sensors"][0]["state"] == "normal")
            assert one_hot["fan"]["duty_percent"] == fixture.pwm()["duty_percent"] == 75
            inject("temperature 1 60000")
            cool = wait_for(lambda value: value["fan"]["duty_percent"] == 50)
            assert all(sensor["state"] == "normal" for sensor in cool["sensors"])
            stale = wait_for(lambda value: all(sensor["state"] == "stale" for sensor in value["sensors"]))
            assert all(sensor["temperature_millicelsius"] is None for sensor in stale["sensors"])
            assert stale["fan"]["duty_percent"] == fixture.pwm()["duty_percent"] == 75
            inject("temperature 0 42000", "temperature 1 55000")
            fresh = wait_for(lambda value: value["fan"]["duty_percent"] == 50)
            assert fixture.outputs() == 0
            fixture.tach(8)
            healthy = wait_for(lambda value: value["fan"]["rpm"] is not None)
            assert 2000 <= healthy["fan"]["rpm"] <= 4500, healthy
            assert healthy["fan"]["protected"] is False
            inject("temperature 42000", "start")
            starting = wait_for(lambda value: value["power"]["state"] == "starting")
            assert fixture.outputs() & 1
            assert starting["power_attempt"] == 1
            first_wait = wait_for(lambda value: value["power"]["state"] == "retry_wait")
            assert first_wait["power_attempt"] == 1 and fixture.outputs() & 1 == 0
            fixture.level(5, True)
            backoff_edge = wait_for(lambda value: value["power_good_edges"] == 1)
            assert backoff_edge["power"]["state"] == "retry_wait"
            retry_two = wait_for(lambda value: value["power_attempt"] == 2
                                 and value["power"]["state"] == "starting")
            assert fixture.outputs() & 1 == 1
            second_wait = wait_for(lambda value: value["power_attempt"] == 2
                                  and value["power"]["state"] == "retry_wait")
            assert fixture.outputs() & 1 == 0
            fixture.level(5, False)
            retry_three = wait_for(lambda value: value["power_attempt"] == 3
                                   and value["power"]["state"] == "starting")
            assert fixture.outputs() & 1 == 1
            time.sleep(0.13)  # Withhold both signals and HTTP requests.
            failed = wait_for(lambda value: value["power"]["state"] == "failed")
            assert failed["power"]["enabled"] is False
            assert failed["power_attempt"] == failed["power_attempt_limit"] == 3
            assert failed["sensor"]["temperature_millicelsius"] == 42000
            assert failed["fan"] == {"rpm": None, "protected": True, "duty_percent": 100}
            assert fixture.outputs() == 2
            protected_pwm = fixture.pwm()
            assert protected_pwm["levels"] == [True] * 5
            inject("power-good", "tach 3000", "temperature 43000")
            serial_only = wait_for(lambda value: value["sensor"]["temperature_millicelsius"] == 43000)
            assert serial_only["power"] == failed["power"]
            assert serial_only["fan"] == failed["fan"]
            assert serial_only["power_good_edges"] == failed["power_good_edges"]
            fixture.level(5, True)
            late = wait_for(lambda value: value["power_good_edges"] == failed["power_good_edges"] + 1)
            assert late["power"] == failed["power"]
            assert fixture.outputs() == 2
            fixture.level(5, False)
            assert request("POST")[0] == 405
            assert request()[1] == late
            inject("start")
            wait_for(lambda value: value["power"]["state"] == "starting")
            assert fixture.outputs() == 3
            fixture.level(5, True)
            recovered = wait_for(lambda value: value["power"]["state"] == "on")
            assert recovered["power"]["enabled"] is True
            assert recovered["power_attempt"] == 1
            assert recovered["fan"]["protected"] is True
            assert recovered["power_good_edges"] == late["power_good_edges"] + 1
            assert fixture.outputs() == 3
            code, journal = request(path="/v1/events")
            assert code == 200
            assert journal["revision"] == recovered["revision"]
            assert journal["overwritten"] == 0
            events = journal["events"]
            assert len(events) == 22
            assert [event["sequence"] for event in events] == list(range(1, 23))
            for sensor_id in (0, 1):
                assert [e["kind"] for e in events if e["sensor_id"] == sensor_id] == [
                    "sensor_high", "sensor_cleared", "sensor_stale", "sensor_recovered",
                ]
            kinds = [event["kind"] for event in events if event["sensor_id"] is None]
            assert kinds.count("fan_protection") == 1
            assert [kind for kind in kinds if kind != "fan_protection"] == [
                "power_started", "power_timed_out", "power_retry_scheduled", "power_good_ignored",
                "power_retry_started", "power_timed_out", "power_retry_scheduled",
                "power_retry_started", "power_timed_out", "power_locked", "power_good_ignored",
                "power_started", "power_on",
            ]
            scheduled = [e["at_ms"] for e in events if e["kind"] == "power_retry_scheduled"]
            retried = [e["at_ms"] for e in events if e["kind"] == "power_retry_started"]
            assert all(retry - planned >= 100 for planned, retry in zip(scheduled, retried))
            assert all(a["revision"] < b["revision"] and a["at_ms"] <= b["at_ms"]
                       for a, b in zip(events, events[1:]))
            assert next(e["revision"] for e in events if e["kind"] == "power_timed_out") <= failed["revision"]
            assert max(e["revision"] for e in events if e["kind"] == "power_good_ignored") == late["revision"]
            assert events[-1]["revision"] == recovered["revision"]
            assert request("POST", "/v1/events")[0] == 405
            assert request(path="/v1/events") == (200, journal)
            (output / f"{guest}-events.json").write_text(json.dumps(journal, indent=2) + "\n")
            snapshots = dict(initial=initial, hot=hot, hysteresis=hysteresis, one_hot=one_hot,
                             cool=cool, stale=stale, fresh=fresh, healthy=healthy, starting=starting,
                             first_retry_wait=first_wait, second_attempt=retry_two,
                             second_retry_wait=second_wait, third_attempt=retry_three,
                             timed_out=failed, serial_bypass_rejected=serial_only,
                             late_feedback=late, restarted=recovered, normal_pwm=normal_pwm,
                             protected_pwm=protected_pwm)
            (output / f"{guest}-snapshots.json").write_text(json.dumps(snapshots, indent=2) + "\n")
            print(f"{guest}: three attempts, GPIO lockout, thermal regression and 22 events verified", flush=True)
        finally:
            if fixture is not None:
                fixture.close()
            if vm.poll() is None:
                vm.terminate()
                try:
                    vm.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    vm.kill()


if __name__ == "__main__":
    qemu, kernel, output = sys.argv[1:]
    for guest in ("linux", "arceos"):
        for retry_success in (False, True):
            with tempfile.TemporaryDirectory(prefix="bmc-gpio-", dir="/tmp") as directory:
                run(qemu, kernel, Path(output), guest, Path(directory), retry_success)

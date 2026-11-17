"""Verify BMC-G1 sensors via a real OpenBMC ObjectMapper and bmcweb."""
import argparse
import base64
from configparser import ConfigParser
from http.client import HTTPException, HTTPSConnection
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import json
from pathlib import Path
import secrets
import re
import ssl
import subprocess
import sys
import tempfile
import time
from threading import Barrier, BrokenBarrierError, Event

from appliance import provision, publish, stop_process
from gateway import Gateway
from verify import run
from verify_gateway import serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "kernel", "images", "output"):
        parser.add_argument(name)
    parser.add_argument("--bus-address", required=True)
    parser.add_argument("--bmcweb-port", type=int, required=True)
    parser.add_argument("--credentials", type=Path, required=True, help="file containing username:password")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--bmcweb-ca-file")
    parser.add_argument("--insecure-test-bmcweb", action="store_true",
                        help="accept the stock certificate on an isolated loopback QEMU image")
    parser.add_argument("--validator", help="DMTF rf_service_validator executable")
    parser.add_argument("--schema-directory", type=Path)
    parser.add_argument("--power", action="store_true", help="test power with Romulus state units stopped")
    parser.add_argument("--native-state-manager", action="store_true",
                        help="test native state services with the Romulus power driver replaced")
    parser.add_argument("--conformance", action="store_true",
                        help="test Redfish sessions and validate connected host and chassis resources")
    parser.add_argument("--protocol-validator",
                        help="run DMTF Protocol Validator against the dedicated Romulus guest")
    parser.add_argument("--account-etag", action="store_true",
                        help="check conditional account writes over preconnected TLS sockets")
    parser.add_argument("--native-power-loss", action="store_true",
                        help="inject sustained power-good loss in native state-manager mode")
    parser.add_argument("--interop-validator", help="DMTF rf_interop_validator executable")
    parser.add_argument("--interop-profile", type=Path, default=Path(__file__).with_name("profiles")
                        / "BmcG1Management.v1_0_0.json")
    args = parser.parse_args()
    if args.native_state_manager and args.power:
        parser.error("choose one power integration mode")
    if args.validator and not args.schema_directory:
        parser.error("--validator requires --schema-directory")
    if args.conformance and not ((args.power or args.native_state_manager) and args.validator):
        parser.error("--conformance requires a power integration mode and --validator")
    if args.protocol_validator and not args.conformance:
        parser.error("--protocol-validator requires --conformance")
    if args.interop_validator and not args.conformance:
        parser.error("--interop-validator requires --conformance")
    if args.account_etag and not args.conformance:
        parser.error("--account-etag requires --conformance")
    if args.native_power_loss and not args.native_state_manager:
        parser.error("--native-power-loss requires --native-state-manager")
    output = Path(args.output)
    output.mkdir()
    # The stock isolated QEMU image uses a self-signed certificate.
    context = (ssl._create_unverified_context() if args.insecure_test_bmcweb
               else ssl.create_default_context(cafile=args.bmcweb_ca_file))
    credentials = args.credentials.read_text().strip()
    auth = base64.b64encode(credentials.encode()).decode()

    def connection():
        return HTTPSConnection("127.0.0.1", args.bmcweb_port, timeout=5, context=context)

    def exchange(path, body=None, *, method=None, authorization=None):
        conn = connection()
        try:
            headers = {"Content-Type": "application/json"}
            if authorization is not None:
                headers.update(authorization)
            conn.request(method or ("POST" if body is not None else "GET"), path,
                         body=json.dumps(body) if body is not None else None, headers=headers)
            reply = conn.getresponse()
            data = reply.read()
            return reply.status, reply.headers, json.loads(data) if data else None
        finally:
            conn.close()

    def resource(path, body=None):
        code, _, value = exchange(path, body, authorization={"Authorization": "Basic " + auth})
        return code, value

    def validate(directory, targets):
        username, password = credentials.split(":", 1)
        exits = {}
        for name, path in targets.items():
            with (directory / f"validator-{name}.log").open("wb") as log:
                exits[name] = subprocess.run([
                    args.validator, "-r", f"https://127.0.0.1:{args.bmcweb_port}",
                    "-u", username, "-p", password, "--authtype", "Session" if args.conformance else "Basic",
                    "--payload", "Single", path, "--skipschema",
                    "--schema_directory", str(args.schema_directory),
                    "--logdir", str(directory / "validator" / name),
                ], stdout=log, stderr=subprocess.STDOUT, timeout=60).returncode
        return exits

    def await_resource(path, predicate):
        deadline = time.monotonic() + 10
        last = None
        while time.monotonic() < deadline:
            try:
                code, value = resource(path)
                if code == 200 and predicate(value):
                    return value
                last = (code, value)
            except (TimeoutError, ConnectionError) as error:
                last = str(error)
            time.sleep(0.1)
        raise AssertionError((path, last))

    for guest in ("linux", "arceos"):
        directory = output / guest
        directory.mkdir()

        def workload(vm, fixture, request, wait_for, port):
            reader, operator = secrets.token_hex(24), secrets.token_hex(24)
            assert provision(vm, port, reader, operator, secrets.token_hex(16), Event())
            with tempfile.TemporaryDirectory(prefix="bmc-upstream-token-") as tmp:
                token = Path(tmp) / "reader"
                publish(token, reader)
                operator_token = Path(tmp) / "operator"
                publish(operator_token, operator)
                with serve(Gateway(("127.0.0.1", 0), port, args.cert, args.key)) as tls_port:
                    bridge = subprocess.Popen([
                        sys.executable, str(Path(__file__).with_name("openbmc_sensors.py")),
                        f"https://127.0.0.1:{tls_port}", "--token-file", str(token),
                        "--ca-file", args.cert, "--bus-address", args.bus_address,
                        "--external-mapper", "--interval", "0.02",
                        *(["--upstream-power", "--operator-token-file", str(operator_token)]
                          if args.power else ["--sensors-only"]),
                        *(["--romulus-power", "--operator-token-file", str(operator_token)]
                          if args.native_state_manager else []),
                    ])
                    try:
                        chassis = "/redfish/v1/Chassis/G1"
                        await_resource(chassis, lambda value: value["Id"] == "G1")
                        collection = await_resource(chassis + "/Sensors", lambda value: bool(value["Members"]))
                        paths = [member["@odata.id"] for member in collection["Members"]]
                        board = next(path for path in paths if path.endswith("board"))
                        def sample_board(value):
                            vm.stdin.write(b"temperature 1 55250\n")
                            vm.stdin.flush()
                            return value.get("Reading") == 55.25

                        vm.stdin.write(b"temperature 1 55250\n")
                        vm.stdin.flush()
                        reading = await_resource(board, sample_board)
                        assert len(paths) == 3 and any("fan" in path for path in paths)
                        stale = await_resource(board, lambda value: value.get("Reading") is None)
                        power = None
                        if args.power and not args.conformance:
                            power = verify_power(vm, fixture, request, reader, resource, await_resource)
                        if args.native_state_manager and not args.conformance:
                            power = verify_native_power(vm, fixture, request, reader, resource, args.native_power_loss)
                        validator_exit = None
                        conformance = None
                        if args.conformance:
                            with test_sessions(exchange, credentials, fixture,
                                               connection if args.account_etag else None) as (operator_resource, sessions):
                                if args.native_state_manager:
                                    power = verify_native_power(vm, fixture, request, reader, operator_resource,
                                                                args.native_power_loss)
                                else:
                                    power = verify_power(vm, fixture, request, reader, operator_resource, await_resource)
                            conformance = sessions
                        if args.validator:
                            targets = {"board": board}
                            if args.conformance:
                                targets.update(system="/redfish/v1/Systems/system", chassis=chassis,
                                               sensors=chassis + "/Sensors", session="/redfish/v1/SessionService",
                                               fan=next(path for path in paths if "fantach_" in path))
                            validator_exit = validate(directory, targets)
                        protocol = None
                        if args.protocol_validator:
                            protocol = validate_protocol(args, directory, Path(tmp), credentials)
                        interop = validate_interop(args, directory, credentials) if args.interop_validator else None
                    finally:
                        stop_process(bridge)
                    deadline = time.monotonic() + 10
                    while resource("/redfish/v1/Chassis/G1")[0] != 404:
                        assert time.monotonic() < deadline
                        time.sleep(0.1)
            (directory / "upstream.json").write_text(json.dumps(
                dict(chassis=chassis, sensors=paths, reading=reading, stale=stale,
                     removed_on_disconnect=True, validator_exit=validator_exit, power=power,
                     conformance=conformance, protocol=protocol, interop=interop),
                indent=2,
            ) + "\n")

        with tempfile.TemporaryDirectory(prefix="bmc-upstream-", dir="/tmp") as sockets:
            run(args.qemu, args.kernel, directory, guest, Path(sockets),
                images=Path(args.images), workload=workload)
        print(f"{guest}: upstream mapper and bmcweb sensor verified", flush=True)
    return summarize(output)


def summarize(output):
    """Record both backends before returning the requested validators' verdict."""
    reports = {}
    passed = True
    for guest in ("linux", "arceos"):
        result = json.loads((output / guest / "upstream.json").read_text())
        reports[guest] = {key: result[key] for key in (
            "power", "conformance", "validator_exit", "protocol", "interop",
        )}
        if result["validator_exit"] is not None:
            passed &= all(code == 0 for code in result["validator_exit"].values())
        account = (result["conformance"] or {}).get("account_etag")
        if account is not None:
            passed &= account["passed"]
        recovery = (result["power"] or {}).get("unexpected_loss")
        if recovery is not None:
            passed &= recovery["passed"]
        for key in ("protocol", "interop"):
            counts = result[key]
            if counts is not None:
                passed &= counts["exit_code"] == 0 and counts["fail"] == 0 and counts["pass"] > 0
    report = {"passed": passed, **reports}
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    return int(not passed)


def validate_interop(args, directory, credentials):
    username, password = credentials.split(":", 1)
    log = directory / "interop.log"
    with log.open("wb") as stream:
        result = subprocess.run([
            args.interop_validator, "--rhost", f"https://127.0.0.1:{args.bmcweb_port}",
            "--user", username, "--password", password, "--authtype", "Session",
            "--timeout", "5", "--logdir", str(directory / "interop"), str(args.interop_profile),
        ], stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    output = re.sub(r"\x1b\[[0-9;]*m", "", log.read_text())
    match = re.search(r"\|\s*PASS\s*\|\s*WARN\s*\|\s*FAIL\s*\|\s*NOT TESTED\s*\|"
                      r"[^\n]*\n[^\n]*\n\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)",
                      output)
    if not match:
        raise RuntimeError(f"Interop Validator did not produce a summary; see {log}")
    return dict(exit_code=result.returncode,
                **dict(zip(("pass", "warn", "fail", "skip"), map(int, match.groups()))))


def validate_protocol(args, directory, temporary, credentials):
    username, password = credentials.split(":", 1)
    config = ConfigParser(interpolation=None)
    config["Authentication"] = {"user": username, "password": password}
    config_path = temporary / "protocol.ini"
    with config_path.open("w") as stream:
        config.write(stream)
    reports = directory.resolve() / "protocol"
    command = [
        args.protocol_validator, "--config", str(config_path), "--rhost",
        f"https://127.0.0.1:{args.bmcweb_port}", "--avoid-http-redirect",
        "--report-dir", str(reports), "--report-type", "both",
    ]
    if args.insecure_test_bmcweb:
        command.append("--no-cert-check")
    elif args.bmcweb_ca_file:
        command.extend(["--ca-bundle", args.bmcweb_ca_file])
    with (directory / "protocol.log").open("wb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=180)
    summaries = list(reports.rglob("*.json"))
    if len(summaries) != 1:
        raise RuntimeError(f"Protocol Validator did not produce one summary; see {log.name}")
    summary = json.loads(summaries[0].read_text())
    counts = summary["TestResults"]["Protocol Validations"]
    return dict(tool=summary["ToolName"], exit_code=result.returncode, **counts)


def verify_native_power(vm, fixture, request, reader, resource, test_loss=False):
    system = "/redfish/v1/Systems/system"
    reset = system + "/Actions/ComputerSystem.Reset"
    headers = {"Authorization": f"Bearer {reader}"}

    last_sample = 0

    def keep_alive():
        nonlocal last_sample
        if time.monotonic() - last_sample >= 0.15:
            vm.stdin.write(b"temperature 1 55000\n")
            vm.stdin.flush()
            fixture.level(6, True)
            fixture.level(6, False)
            last_sample = time.monotonic()

    def drive(enabled):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            keep_alive()
            if bool(fixture.outputs() & 1) == enabled:
                fixture.level(5, enabled)
                return
            time.sleep(0.005)
        raise AssertionError(f"native transition did not set GPIO to {enabled}")

    def wait_runtime(expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            keep_alive()
            try:
                code, snapshot = request(headers=headers)
                if code == 200 and snapshot["power"]["state"] == expected:
                    return snapshot
            except (TimeoutError, ConnectionError):
                pass
            time.sleep(0.02)
        raise AssertionError(f"runtime did not reach {expected}")

    def wait_redfish(worker, expected, timeout=12):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            result = worker.submit(resource, system)
            while not result.done():
                keep_alive()
                time.sleep(0.01)
            try:
                code, snapshot = result.result()
            except (OSError, HTTPException):
                continue
            if code == 200:
                last = snapshot.get("PowerState")
                if last == expected:
                    break
            keep_alive()
            time.sleep(0.05)
        return last

    statuses = {}
    confirmed = {}
    with ThreadPoolExecutor(max_workers=1) as worker:
        def reset_power(action, enabled):
            future = worker.submit(resource, reset, {"ResetType": action})
            if action == "PowerCycle":
                drive(False)
            drive(enabled)
            statuses[action], body = future.result()
            assert statuses[action] in (200, 202, 204), body
            wait_runtime("on" if enabled else "off")
            expected = "On" if enabled else "Off"
            confirmed[action] = wait_redfish(worker, expected)
            assert confirmed[action] == expected, confirmed[action]

        for action, enabled in (("On", True), ("PowerCycle", True)):
            reset_power(action, enabled)

        recovery = None
        if test_loss:
            fixture.level(5, False)
            lost = wait_runtime("power_good_lost")
            assert not lost["power"]["enabled"] and not (fixture.outputs() & 1)
            reported = wait_redfish(worker, "Off", timeout=6)
            deadline = time.monotonic() + 3
            while not (fixture.outputs() & 1) and time.monotonic() < deadline:
                keep_alive()
                time.sleep(0.01)
            automatic = bool(fixture.outputs() & 1)
            if automatic:
                drive(True)
                wait_runtime("on")
                restored = wait_redfish(worker, "On")
            else:
                restored = None
            recovery = dict(passed=reported == "Off" and automatic and restored == "On",
                            redfish_at_loss=reported, automatic_restart=automatic,
                            restart_window_seconds=3, redfish_after_recovery=restored)
        if not test_loss or recovery["automatic_restart"]:
            reset_power("ForceOff", False)
    code, journal = request(path="/v1/events", headers=headers)
    assert code == 200
    expected = ["power_started", "power_on", "power_stopped", "power_started", "power_on"]
    if test_loss:
        expected.append("power_good_lost")
        if recovery["automatic_restart"]:
            expected.extend(["power_started", "power_on"])
    if not test_loss or recovery["automatic_restart"]:
        expected.append("power_stopped")
    events = [event["kind"] for event in journal["events"]]
    power_events = {"power_started", "power_on", "power_stopped", "power_good_lost"}
    assert [event for event in events if event in power_events] == expected, events
    return dict(native_state_manager=True, statuses=statuses, confirmed_power=confirmed, events=expected,
                **({"unexpected_loss": recovery} if test_loss else {}))


def verify_account_etag(exchange, connection, path, auth):
    code, headers, account = exchange(path, authorization=auth)
    assert code == 200 and account["Enabled"] is True, code
    tag = headers.get("ETag")
    if not tag:
        return dict(passed=False, error="missing HTTP ETag")
    stale, _, _ = exchange(path, {"Enabled": False}, method="PATCH",
                           authorization={**auth, "If-Match": '"0000000000000000"'})
    barrier = Barrier(4, timeout=6)

    def patch(conn):
        try:
            barrier.wait()
            conn.request("PATCH", path, json.dumps({"Enabled": False}), headers={
                **auth, "If-Match": tag, "Content-Type": "application/json",
            })
            reply = conn.getresponse()
            reply.read()
            return {"status": reply.status}
        except (OSError, HTTPException, BrokenBarrierError) as error:
            barrier.abort()
            return {"error": str(error) or type(error).__name__}

    with ExitStack() as clients:
        connections = []
        for _ in range(4):
            for attempt in range(3):
                conn = connection()
                try:
                    conn.connect()
                except (OSError, HTTPException) as error:
                    conn.close()
                    if attempt == 2:
                        return dict(passed=False, stale_status=stale, writes=[],
                                    error=f"TLS setup: {error}",
                                    payload_etag_matches=account.get("@odata.etag") == tag)
                    time.sleep(0.2)
                else:
                    connections.append(clients.enter_context(closing(conn)))
                    break
        with ThreadPoolExecutor(max_workers=4) as workers:
            replies = list(workers.map(patch, connections))
    code, after_headers, after = exchange(path, authorization=auth)
    updated = code == 200 and after["Enabled"] is False and after_headers.get("ETag") not in (None, tag)
    statuses = [reply.get("status") for reply in replies]
    passed = stale == 412 and statuses.count(204) == 1 and statuses.count(412) == 3 and updated
    return dict(passed=passed, stale_status=stale, writes=replies, updated=updated,
                payload_etag_matches=account.get("@odata.etag") == tag)


@contextmanager
def test_sessions(exchange, credentials, fixture, account_connection=None):
    username, password = credentials.split(":", 1)
    sessions = "/redfish/v1/SessionService/Sessions"
    system = "/redfish/v1/Systems/system"
    reset = system + "/Actions/ComputerSystem.Reset"

    def login(user, secret):
        status, headers, _ = exchange(sessions, {"UserName": user, "Password": secret})
        assert status == 201, status
        return {"X-Auth-Token": headers["X-Auth-Token"]}, headers["Location"]

    def status(path, body=None, **kwargs):
        return exchange(path, body, **kwargs)[0]

    assert status(system) == 401
    assert status(reset, {"ResetType": "On"}, authorization={"X-Auth-Token": "invalid"}) == 401
    admin, admin_path = login(username, password)
    account_path = None
    try:
        user = "g1" + secrets.token_hex(4)
        secret = "G1a!" + secrets.token_hex(8)
        code, headers, _ = exchange("/redfish/v1/AccountService/Accounts",
                                    {"UserName": user, "Password": secret, "RoleId": "ReadOnly",
                                     "Enabled": True}, authorization=admin)
        assert code == 201, code
        account_path = headers.get("Location") or f"/redfish/v1/AccountService/Accounts/{user}"
        reader, reader_path = login(user, secret)
        try:
            assert status(system, authorization=reader) == 200
            assert status(reset, {"ResetType": "On"}, authorization=reader) == 403
            assert fixture.outputs() & 1 == 0
        finally:
            assert status(reader_path, method="DELETE", authorization=reader) in (200, 204)
        assert status(system, authorization=reader) == 401

        def operator_resource(path, body=None):
            code, _, value = exchange(path, body, authorization=admin)
            return code, value

        report = dict(anonymous=401, invalid_token=401, reader_get=200,
                      reader_reset=403, reader_after_logout=401)
        yield operator_resource, report
        if account_connection is not None:
            report["account_etag"] = verify_account_etag(exchange, account_connection, account_path, admin)
    finally:
        if account_path is not None:
            assert status(account_path, method="DELETE", authorization=admin) in (200, 204)
        assert status(admin_path, method="DELETE", authorization=admin) in (200, 204)
        assert status(system, authorization=admin) == 401


def verify_power(vm, fixture, request, reader, resource, await_resource):
    system = "/redfish/v1/Systems/system"
    reset = system + "/Actions/ComputerSystem.Reset"
    await_resource(system, lambda value: value.get("PowerState") == "Off")

    def gpio(enabled):
        deadline = time.monotonic() + 4
        while bool(fixture.outputs() & 1) != enabled:
            assert time.monotonic() < deadline, f"GPIO did not reach {enabled}"
            time.sleep(0.003)

    def temperature(value):
        vm.stdin.write(f"temperature 1 {value}\n".encode())
        vm.stdin.flush()
        deadline = time.monotonic() + 3
        while True:
            code, state = request(headers={"Authorization": f"Bearer {reader}"})
            if code == 200 and state["sensors"][1]["temperature_millicelsius"] == value:
                return
            assert time.monotonic() < deadline
            time.sleep(0.01)

    statuses = {}
    temperature(55000)
    with ThreadPoolExecutor(max_workers=1) as worker:
        for action in ("On", "PowerCycle"):
            response = worker.submit(resource, reset, {"ResetType": action})
            if action == "PowerCycle":
                gpio(False)
                fixture.level(5, False)
            gpio(True)
            fixture.level(5, True)
            statuses[action], response_body = response.result()
            assert statuses[action] in (200, 204), response_body
            await_resource(system, lambda value: value.get("PowerState") == "On")
            fixture.level(6, True)
            fixture.level(6, False)
    statuses["ForceOff"], response_body = resource(reset, {"ResetType": "ForceOff"})
    assert statuses["ForceOff"] in (200, 204), response_body
    gpio(False)
    await_resource(system, lambda value: value.get("PowerState") == "Off")
    temperature(85000)
    rejected, response_body = resource(reset, {"ResetType": "On"})
    assert 400 <= rejected < 600, response_body
    gpio(False)
    code, journal = request(path="/v1/events", headers={"Authorization": f"Bearer {reader}"})
    assert code == 200
    events = [event["kind"] for event in journal["events"]]
    expected = ["power_started", "power_on", "power_cycle_scheduled", "power_cycle_started",
                "power_on", "power_stopped", "thermal_start_blocked"]
    assert [event for event in events if event in expected] == expected, events
    return dict(statuses=statuses, thermal_http_status=rejected, events=expected)


if __name__ == "__main__":
    sys.exit(main())

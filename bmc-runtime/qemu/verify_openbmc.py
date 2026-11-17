"""Exercise the OpenBMC sensor bridge using a real private D-Bus and both guests."""
import argparse
import asyncio
from contextlib import closing
import json
import math
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from threading import Event

from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

from appliance import provision, publish, stop_process
from gateway import Gateway
from openbmc_sensors import ASSOCIATION, ASSOCIATIONS, CHASSIS, INVENTORY_ROOT, ITEM, MAPPER, MAPPER_PATH, ROOT, SERVICE, VALUE
from openbmc_sensors import AVAILABILITY, CHASSIS_PATH, CHASSIS_STATE, HOST_PATH, HOST_STATE, TRANSITION_PREFIX
from verify import run
from verify_gateway import serve


async def check(address, vm, fixture):
    bus = await MessageBus(bus_address=address).connect()

    async def proxy(path, service=SERVICE):
        deadline = time.monotonic() + 5
        while True:
            try:
                node = await bus.introspect(service, path)
                return bus.get_proxy_object(service, path, node)
            except DBusError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    try:
        manager = (await proxy(ROOT)).get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
        paths = [f"{ROOT}/temperature/{name}" for name in ("board", "inlet")]
        fan_path = f"{ROOT}/fan_tach/fan0"
        sensor_paths = [fan_path, *paths]
        assert sorted(path for path, interfaces in objects.items() if VALUE in interfaces) == sensor_paths
        assert all(objects[path][VALUE]["Unit"].value == VALUE + ".Unit.DegreesC" for path in paths)
        assert objects[fan_path][VALUE]["Unit"].value == VALUE + ".Unit.RPMS"
        inventory = (await proxy(INVENTORY_ROOT)).get_interface("org.freedesktop.DBus.ObjectManager")
        items = await inventory.call_get_managed_objects()
        for path in sensor_paths:
            assert objects[path][ASSOCIATIONS]["Associations"].value == [["inventory", "sensors", CHASSIS]]
        assert items[CHASSIS][ITEM]["Present"].value is True
        mapper = (await proxy(MAPPER_PATH, MAPPER)).get_interface(MAPPER)
        assert VALUE in (await mapper.call_get_object(fan_path, [VALUE]))[SERVICE]
        assert await mapper.call_get_sub_tree_paths(ROOT, 1, [VALUE]) == []
        assert await mapper.call_get_sub_tree_paths(ROOT, 0, [VALUE]) == sensor_paths
        forward = CHASSIS + "/sensors"
        assert await mapper.call_get_associated_sub_tree_paths(forward, ROOT, 0, [VALUE]) == sensor_paths
        assert list(await mapper.call_get_associated_sub_tree(fan_path + "/inventory", INVENTORY_ROOT, 0, [ITEM])) == [CHASSIS]
        endpoints = (await proxy(forward, MAPPER)).get_interface(ASSOCIATION)
        assert await endpoints.get_endpoints() == sensor_paths
        fan = (await proxy(fan_path)).get_interface(VALUE)
        assert math.isnan(await fan.get_value())
        # Poll the bridge while producing GPIO pulses long enough to observe a live sample.
        deadline = time.monotonic() + 3
        while math.isnan(rpm := await fan.get_value()):
            assert time.monotonic() < deadline
            await asyncio.to_thread(fixture.tach, 8)
        assert 1000 <= rpm <= 10000
        sensor = await proxy(paths[0])
        properties = sensor.get_interface("org.freedesktop.DBus.Properties")
        value = sensor.get_interface(VALUE)
        assert math.isnan(await value.get_value())
        changes = []

        def on_change(interface, changed, invalidated):
            if interface == VALUE and "Value" in changed:
                changes.append(changed["Value"].value)

        properties.on_properties_changed(on_change)
        vm.stdin.write(b"temperature 1 55250\n")
        vm.stdin.flush()

        async def expect(predicate):
            deadline = time.monotonic() + 4
            while not predicate(await value.get_value()):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)

        await expect(lambda reading: reading == 55.25)
        await expect(math.isnan)
        assert math.isnan(await fan.get_value())
        assert changes[0] == 55.25 and math.isnan(changes[-1])
        from dbus_next import Variant
        try:
            await properties.call_set(VALUE, "Value", Variant("d", 10.0))
        except DBusError as error:
            assert error.type == "org.freedesktop.DBus.Error.PropertyReadOnly"
        else:
            raise AssertionError("sensor write succeeded")
        assert HOST_STATE in (await mapper.call_get_object(HOST_PATH, [HOST_STATE]))[SERVICE]
        assert list(await mapper.call_get_associated_sub_tree(
            CHASSIS_PATH + "/monitoring", INVENTORY_ROOT, 0, [ITEM])) == [CHASSIS]
        host = await proxy(HOST_PATH)
        host_state = host.get_interface(HOST_STATE)
        chassis_state = (await proxy(CHASSIS_PATH)).get_interface(CHASSIS_STATE)
        assert await host.get_interface(AVAILABILITY).get_available()
        power_changes = []

        def on_power_change(interface, changed, invalidated):
            if interface == HOST_STATE and "CurrentHostState" in changed:
                power_changes.append(changed["CurrentHostState"].value.rsplit(".", 1)[-1])

        host.get_interface("org.freedesktop.DBus.Properties").on_properties_changed(on_power_change)

        async def expect_power(host_expected, chassis_expected):
            deadline = time.monotonic() + 2
            while True:
                if (await host_state.get_current_host_state() == HOST_STATE + ".HostState." + host_expected
                        and await chassis_state.get_current_power_state() == CHASSIS_STATE + ".PowerState." + chassis_expected):
                    return
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)

        await expect_power("Off", "Off")
        vm.stdin.write(b"temperature 1 55000\n")
        vm.stdin.flush()
        await expect(lambda reading: reading == 55)
        await host_state.set_requested_host_transition(TRANSITION_PREFIX + "On")
        await expect_power("TransitioningToRunning", "TransitioningToOn")
        while not fixture.outputs() & 1:
            await asyncio.sleep(0.005)
        fixture.level(5, True)
        await expect_power("Running", "On")
        assert fixture.outputs() & 1
        await host_state.set_requested_host_transition(TRANSITION_PREFIX + "Reboot")
        assert not fixture.outputs() & 1
        fixture.level(5, False)
        await expect_power("TransitioningToRunning", "TransitioningToOn")
        while not fixture.outputs() & 1:
            await asyncio.sleep(0.005)
        fixture.level(5, True)
        await expect_power("Running", "On")
        await host_state.set_requested_host_transition(TRANSITION_PREFIX + "Off")
        await expect_power("Off", "Off")
        assert not fixture.outputs() & 1
        assert power_changes == ["TransitioningToRunning", "Running", "TransitioningToRunning", "Running", "Off"]
        vm.stdin.write(b"temperature 1 85000\n")
        vm.stdin.flush()
        await expect(lambda reading: reading == 85)
        try:
            await host_state.set_requested_host_transition(TRANSITION_PREFIX + "On")
        except DBusError as error:
            assert error.type == "xyz.openbmc_project.Common.Error.NotAllowed"
        else:
            raise AssertionError("thermal protection did not reject the operation")
        assert not fixture.outputs() & 1
        return dict(paths=[fan_path, *paths], temperature_c=55.25, fan_rpm=rpm,
                    chassis=CHASSIS, stale_is_nan=True, signal_verified=True, read_only=True,
                    object_mapper=True, bidirectional_associations=True, power_changes=power_changes,
                    host_transitions=["On", "Reboot", "Off"], thermal_rejection=True)
    finally:
        bus.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qemu", "kernel", "images", "output"):
        parser.add_argument(name)
    parser.add_argument("--dbus-daemon", default="dbus-daemon")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir()
    with tempfile.TemporaryDirectory(prefix="bmc-dbus-", dir="/tmp") as tmp:
        work = Path(tmp)
        cert, key, config = (work / name for name in ("cert.pem", "key.pem", "openssl.cnf"))
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
                          "[dn]\nCN=BMC simulation\n[ext]\nsubjectAltName=IP:127.0.0.1\n")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "1", "-config", str(config), "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        bus_config = work / "bus.conf"
        bus_config.write_text(
            '<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>'
            '<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/>'
            '<allow receive_sender="*"/><allow own="*"/></policy></busconfig>'
        )
        for guest in ("linux", "arceos"):
            directory = output / guest
            directory.mkdir()

            def workload(vm, fixture, request, wait_for, port):
                reader, operator = secrets.token_hex(24), secrets.token_hex(24)
                assert provision(vm, port, reader, operator, secrets.token_hex(16), Event())
                token = work / "reader.token"
                publish(token, reader)
                operator_token = work / "operator.token"
                publish(operator_token, operator)
                with serve(Gateway(("127.0.0.1", 0), port, cert, key)) as tls_port:
                    with subprocess.Popen(
                        [args.dbus_daemon, f"--config-file={bus_config}", "--nofork", "--print-address",
                         f"--address=unix:path={work / 'bus'}"],
                        stdout=subprocess.PIPE, text=True,
                    ) as daemon:
                        try:
                            address = daemon.stdout.readline().strip()
                            assert address.startswith("unix:")
                            with subprocess.Popen([
                                sys.executable, str(Path(__file__).with_name("openbmc_sensors.py")),
                                f"https://127.0.0.1:{tls_port}", "--ca-file", str(cert),
                                "--token-file", str(token), "--bus-address", address, "--interval", "0.02",
                                "--operator-token-file", str(operator_token),
                            ]) as bridge:
                                try:
                                    report = asyncio.run(asyncio.wait_for(check(address, vm, fixture), timeout=12))
                                finally:
                                    stop_process(bridge)
                        finally:
                            stop_process(daemon)
                (directory / "sensors.json").write_text(json.dumps(report, indent=2) + "\n")

            with tempfile.TemporaryDirectory(prefix="bmc-sensors-", dir="/tmp") as sockets:
                run(args.qemu, args.kernel, directory, guest, Path(sockets),
                    images=Path(args.images), workload=workload)
            print(f"{guest}: D-Bus sensor discovery, reading, expiry and read-only verified", flush=True)


if __name__ == "__main__":
    main()

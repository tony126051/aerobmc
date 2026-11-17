"""Expose BMC-G1 temperature readings on an OpenBMC-style D-Bus."""
import argparse
import asyncio
from http.client import HTTPException
import math
from pathlib import Path
import secrets
import time

from dbus_next import Message, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import MessageType, PropertyAccess, RequestNameReply
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, dbus_property, method, signal

from manage import Client

SERVICE = "xyz.openbmc_project.BmcG1.Sensors"
ROOT = "/xyz/openbmc_project/sensors"
VALUE = "xyz.openbmc_project.Sensor.Value"
INVENTORY_ROOT = "/xyz/openbmc_project/inventory"
CHASSIS = INVENTORY_ROOT + "/system/chassis/G1"
ITEM = "xyz.openbmc_project.Inventory.Item"
ASSOCIATIONS = "xyz.openbmc_project.Association.Definitions"
ASSOCIATION = "xyz.openbmc_project.Association"
MAPPER = "xyz.openbmc_project.ObjectMapper"
MAPPER_PATH = "/xyz/openbmc_project/object_mapper"
HOST_STATE = "xyz.openbmc_project.State.Host"
CHASSIS_STATE = "xyz.openbmc_project.State.Chassis"
HOST_PATH = "/xyz/openbmc_project/state/host0"
CHASSIS_PATH = "/xyz/openbmc_project/state/chassis0"
AVAILABILITY = "xyz.openbmc_project.State.Decorator.Availability"
LEGACY_POWER = "org.openbmc.control.Power"
LEGACY_POWER_PATH = "/org/openbmc/control/power0"
SENSORS = {
    "inlet": ("temperature", "DegreesC", -40.0, 150.0),
    "board": ("temperature", "DegreesC", -40.0, 150.0),
    "fan0": ("fan_tach", "RPMS", 0.0, float("inf")),
}

class Association(ServiceInterface):
    def __init__(self, endpoints):
        super().__init__(ASSOCIATION)
        self.paths = endpoints

    @dbus_property(access=PropertyAccess.READ, name="endpoints")
    def endpoints(self) -> "ao":
        return self.paths


class Mapper(ServiceInterface):
    def __init__(self, objects, links):
        super().__init__(MAPPER)
        self.objects = objects
        self.links = links

    def lookup(self, path, interfaces):
        return {service: names for service, names in self.objects.get(path, {}).items()
                if not interfaces or any(name in names for name in interfaces)}

    def subtree(self, root, depth, interfaces, paths=None):
        prefix = root.rstrip("/") + "/"
        if root != "/" and root not in self.objects and not any(p.startswith(prefix) for p in self.objects):
            raise DBusError("xyz.openbmc_project.Common.Error.ResourceNotFound", root)
        if depth < 0:
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs", "negative depth")
        result = {}
        for path in sorted(self.objects if paths is None else paths):
            if path.startswith(prefix) and (not depth or path[len(prefix):].count("/") < depth):
                matches = self.lookup(path, interfaces)
                if matches:
                    result[path] = matches
        return result

    @method()
    def GetObject(self, path: "s", interfaces: "as") -> "a{sas}":
        result = self.lookup(path, interfaces)
        if not result:
            raise DBusError("xyz.openbmc_project.Common.Error.ResourceNotFound", path)
        return result

    @method()
    def GetSubTree(self, root: "s", depth: "i", interfaces: "as") -> "a{sa{sas}}":
        return self.subtree(root, depth, interfaces)

    @method()
    def GetSubTreePaths(self, root: "s", depth: "i", interfaces: "as") -> "as":
        return list(self.subtree(root, depth, interfaces))

    def associated(self, association, root, depth, interfaces):
        if association not in self.links:
            raise DBusError("xyz.openbmc_project.Common.Error.ResourceNotFound", association)
        return self.subtree(root, depth, interfaces, self.links[association])

    @method()
    def GetAssociatedSubTree(self, association: "o", root: "o", depth: "i", interfaces: "as") -> "a{sa{sas}}":
        return self.associated(association, root, depth, interfaces)

    @method()
    def GetAssociatedSubTreePaths(self, association: "s", root: "s", depth: "i", interfaces: "as") -> "as":
        return list(self.associated(association, root, depth, interfaces))


class Chassis(ServiceInterface):
    def __init__(self):
        super().__init__(ITEM)

    @dbus_property(access=PropertyAccess.READ)
    def PrettyName(self) -> "s":
        return "Simulated G1 Chassis"

    @dbus_property(access=PropertyAccess.READ)
    def Present(self) -> "b":
        return True


class InventoryLink(ServiceInterface):
    def __init__(self, forward="inventory", reverse="sensors", endpoints=(CHASSIS,)):
        super().__init__(ASSOCIATIONS)
        self.links = [[forward, reverse, endpoint] for endpoint in endpoints]

    @dbus_property(access=PropertyAccess.READ)
    def Associations(self) -> "a(sss)":
        return self.links


class PowerState(ServiceInterface):
    def __init__(self, interface, enum, property_name):
        super().__init__(interface)
        self.prefix = f"{interface}.{enum}."
        self.property_name = property_name
        self.state = "Off"

    def update(self, state):
        if self.state != state:
            self.state = state
            self.emit_properties_changed({self.property_name: self.prefix + state})


class HostState(PowerState):
    def __init__(self):
        super().__init__(HOST_STATE, "HostState", "CurrentHostState")

    @dbus_property(access=PropertyAccess.READ)
    def CurrentHostState(self) -> "s":
        return self.prefix + self.state


TRANSITIONS = {"On": "start", "Off": "stop", "Reboot": "cycle"}
TRANSITION_PREFIX = HOST_STATE + ".Transition."


class HostControl(HostState):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.requested = TRANSITION_PREFIX + "Off"
        self.request_property = "RequestedHostTransition"
        self.transition_lock = asyncio.Lock()

    @dbus_property(access=PropertyAccess.READ)
    def AllowedHostTransitions(self) -> "as":
        return [TRANSITION_PREFIX + name for name in TRANSITIONS]

    @dbus_property()
    def RequestedHostTransition(self) -> "s":
        return self.requested

    @RequestedHostTransition.setter
    async def RequestedHostTransition(self, transition: "s"):
        await self.apply_transition(transition, TRANSITION_PREFIX, TRANSITIONS)

    async def apply_transition(self, transition, prefix, transitions):
        action = transitions.get(transition[len(prefix):]) if transition.startswith(prefix) else None
        if action is None:
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs", "unsupported host transition")
        if self.transition_lock.locked():
            raise DBusError(HOST_STATE + ".Error.BMCNotReady", "another transition request is pending")
        async with self.transition_lock:
            try:
                result = await asyncio.to_thread(self.submit, action)
            except (OSError, HTTPException, ValueError, KeyError, TypeError) as error:
                raise DBusError("xyz.openbmc_project.Common.Error.Unavailable", "operation result unavailable") from error
            if result == "rejected" or (action == "cycle" and result == "no_op"):
                raise DBusError("xyz.openbmc_project.Common.Error.NotAllowed", "controller rejected transition")
            self.requested = transition
            self.emit_properties_changed({self.request_property: transition})

    def submit(self, action):
        client = Client(self.args.origin, self.args.operator_token_file.read_text().strip(), self.args.ca_file)
        key = secrets.token_hex(16)
        for attempt in range(3):
            try:
                code, _, body = client.request(f"/v1/actions/{action}", b"", key)
                if code not in (502, 503, 504):
                    break
            except (OSError, HTTPException):
                if attempt == 2:
                    raise
            time.sleep(0.01)
        if code != 202 or not isinstance(body, dict) or type(body.get("id")) is not int:
            raise ValueError("transition submission failed")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                code, _, receipt = client.request(f"/v1/actions/{body['id']}")
                if code == 200 and receipt["state"] in ("applied", "rejected", "no_op"):
                    return receipt["state"]
                if code not in (200, 502, 503, 504):
                    raise ValueError("transition receipt unavailable")
            except (OSError, HTTPException):
                pass
            time.sleep(0.02)
        raise ValueError("transition receipt timed out")


class ChassisState(PowerState):
    def __init__(self):
        super().__init__(CHASSIS_STATE, "PowerState", "CurrentPowerState")

    @dbus_property(access=PropertyAccess.READ)
    def CurrentPowerState(self) -> "s":
        return self.prefix + self.state


class ChassisControl(ChassisState):
    prefix_transition = CHASSIS_STATE + ".Transition."
    apply_transition = HostControl.apply_transition
    submit = HostControl.submit

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.transition_lock = asyncio.Lock()
        self.request_property = "RequestedPowerTransition"
        self.requested = self.prefix_transition + "Off"

    @dbus_property()
    def RequestedPowerTransition(self) -> "s":
        return self.requested

    @RequestedPowerTransition.setter
    async def RequestedPowerTransition(self, transition: "s"):
        await self.apply_transition(transition, self.prefix_transition, {"Off": "stop", "On": "start"})


class RomulusPower(ServiceInterface):
    submit = HostControl.submit

    def __init__(self, args):
        super().__init__(LEGACY_POWER)
        self.args = args
        self.good = 0
        self.requested = 0
        self.transition_lock = asyncio.Lock()

    @dbus_property(access=PropertyAccess.READ)
    def pgood(self) -> "i":
        return self.good

    @dbus_property(access=PropertyAccess.READ)
    def state(self) -> "i":
        return self.requested

    @method(name="getPowerState")
    def get_power_state(self) -> "i":
        return self.requested

    @method(name="setPowerState")
    async def set_power_state(self, state: "i"):
        if state not in (0, 1):
            raise DBusError("org.freedesktop.DBus.Error.InvalidArgs", "expected 0 or 1")
        async with self.transition_lock:
            try:
                result = await asyncio.to_thread(self.submit, "start" if state else "stop")
            except (OSError, HTTPException, ValueError, KeyError, TypeError) as error:
                raise DBusError("xyz.openbmc_project.Common.Error.Unavailable", "power operation unavailable") from error
            if result == "rejected":
                raise DBusError("xyz.openbmc_project.Common.Error.NotAllowed", "controller rejected power operation")
            if self.requested != state:
                self.requested = state
                self.emit_properties_changed({"state": state})

    @signal()
    def PowerGood(self):
        pass

    @signal()
    def PowerLost(self):
        pass

    def update(self, power):
        if power is None:
            return
        good = int(power[1] == "On")
        if self.good != good:
            self.good = good
            self.emit_properties_changed({"pgood": good})
            if good:
                self.PowerGood()
            else:
                self.PowerLost()


class Availability(ServiceInterface):
    def __init__(self):
        super().__init__(AVAILABILITY)
        self.available = False

    @dbus_property(access=PropertyAccess.READ)
    def Available(self) -> "b":
        return self.available

    def update(self, available):
        if self.available != available:
            self.available = available
            self.emit_properties_changed({"Available": available})


class Sensor(ServiceInterface):
    def __init__(self, unit, minimum, maximum):
        super().__init__(VALUE)
        self.reading = math.nan
        self.unit = unit
        self.minimum = minimum
        self.maximum = maximum

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "d":
        return self.reading

    @dbus_property(access=PropertyAccess.READ)
    def Unit(self) -> "s":
        return VALUE + ".Unit." + self.unit

    @dbus_property(access=PropertyAccess.READ)
    def MinValue(self) -> "d":
        return self.minimum

    @dbus_property(access=PropertyAccess.READ)
    def MaxValue(self) -> "d":
        return self.maximum

    @dbus_property(access=PropertyAccess.READ)
    def UpdatedTime(self) -> "t":
        return 0

    def update(self, reading):
        if self.reading != reading and not (math.isnan(self.reading) and math.isnan(reading)):
            self.reading = reading
            self.emit_properties_changed({"Value": reading})


def readings(origin, token_file, ca_file):
    client = Client(origin, token_file.read_text().strip(), ca_file)
    status = client.get("/v1/status")
    values = {}
    for sensor in status["sensors"]:
        value = sensor["temperature_millicelsius"]
        if type(value) is int and -40_000 <= value <= 150_000:
            values[sensor["name"]] = value / 1000
    rpm = status["fan"]["rpm"]
    if type(rpm) is int and rpm >= 0:
        values["fan0"] = float(rpm)
    power = status["power"]["state"]
    if power == "on":
        return values, ("Running", "On")
    if power in ("starting", "retry_wait", "cycle_wait"):
        return values, ("TransitioningToRunning", "TransitioningToOn")
    if power in ("off", "failed", "watchdog_tripped", "power_good_lost",
                 "thermal_tripped", "thermal_telemetry_lost"):
        return values, ("Off", "Off")
    raise ValueError("unknown runtime power state")


async def serve(args):
    bus = await MessageBus(bus_address=args.bus_address).connect()
    sensors = {name: Sensor(*spec[1:]) for name, spec in SENSORS.items()}
    bus.export(ROOT, ServiceInterface("org.freedesktop.DBus.ObjectManager"))
    bus.export(INVENTORY_ROOT, ServiceInterface("org.freedesktop.DBus.ObjectManager"))
    bus.export(CHASSIS, Chassis())
    bus.export(CHASSIS, ServiceInterface(ITEM + ".Chassis"))
    objects = {CHASSIS: {SERVICE: [ITEM, ITEM + ".Chassis"]}}
    host = HostControl(args) if args.operator_token_file else HostState()
    chassis = ChassisControl(args) if args.upstream_power else ChassisState()
    states = [] if args.sensors_only else [(HOST_PATH, host, Availability()), (CHASSIS_PATH, chassis, Availability())]
    state_buses = []
    for path, state, availability in states:
        state_bus = await MessageBus(bus_address=args.bus_address).connect() if args.upstream_power else bus
        if args.upstream_power:
            from dbus_next.constants import NameFlag
            if await state_bus.request_name(state.name, NameFlag.DO_NOT_QUEUE) != RequestNameReply.PRIMARY_OWNER:
                state_bus.disconnect()
                for connected in state_buses:
                    connected.disconnect()
                bus.disconnect()
                raise RuntimeError(f"upstream state service name already owned: {state.name}")
            state_buses.append(state_bus)
        state_bus.export(path, state)
        state_bus.export(path, availability)
        objects[path] = {SERVICE: [state.name, AVAILABILITY]}
    if states:
        (state_buses[-1] if state_buses else bus).export(CHASSIS_PATH, InventoryLink("monitoring", "monitored_by"))
        objects[CHASSIS_PATH][SERVICE].append(ASSOCIATIONS)
    links = {CHASSIS + "/sensors": [], CHASSIS_PATH + "/monitoring": [CHASSIS],
             CHASSIS + "/monitored_by": [CHASSIS_PATH]}
    sensors_by_path = {}
    for name, sensor in sensors.items():
        object_name = f"g1_{name}" if args.external_mapper else name
        path = f"{ROOT}/{SENSORS[name][0]}/{object_name}"
        bus.export(path, sensor)
        sensors_by_path[path] = sensor
        bus.export(path, InventoryLink())
        objects[path] = {SERVICE: [VALUE, ASSOCIATIONS]}
        links[path + "/inventory"] = [CHASSIS]
        links[CHASSIS + "/sensors"].append(path)
    if args.external_mapper:
        bus.export(CHASSIS, InventoryLink("all_sensors", "chassis", links[CHASSIS + "/sensors"]))

        def get_all_sensor_properties(message):
            # bmcweb requests properties across interfaces with an empty name.
            if (message.message_type != MessageType.METHOD_CALL
                    or message.interface != "org.freedesktop.DBus.Properties"
                    or message.member != "GetAll" or message.signature != "s"
                    or message.body != [""] or message.path not in sensors_by_path):
                return None
            sensor = sensors_by_path[message.path]
            return Message.new_method_return(message, "a{sv}", [{
                "Value": Variant("d", sensor.reading),
                "Unit": Variant("s", VALUE + ".Unit." + sensor.unit),
                "MinValue": Variant("d", sensor.minimum),
                "MaxValue": Variant("d", sensor.maximum),
                "UpdatedTime": Variant("t", 0),
                "Associations": Variant("a(sss)", [["inventory", "sensors", CHASSIS]]),
            }])

        bus.add_message_handler(get_all_sensor_properties)
    for path, endpoints in links.items():
        if not args.external_mapper:
            bus.export(path, Association(sorted(endpoints)))
        objects[path] = {MAPPER: [ASSOCIATION]}
    if not args.external_mapper:
        bus.export(MAPPER_PATH, Mapper(objects, links))
        if await bus.request_name(MAPPER) != RequestNameReply.PRIMARY_OWNER:
            bus.disconnect()
            raise RuntimeError("ObjectMapper name already owned; use a private simulation bus")
    if await bus.request_name(SERVICE) != RequestNameReply.PRIMARY_OWNER:
        bus.disconnect()
        raise RuntimeError("sensor service name already owned")
    romulus = None
    if args.romulus_power:
        from dbus_next.constants import NameFlag
        power_bus = await MessageBus(bus_address=args.bus_address).connect()
        state_buses.append(power_bus)
        romulus = RomulusPower(args)
        power_bus.export(LEGACY_POWER_PATH, romulus)
        if await power_bus.request_name(LEGACY_POWER, NameFlag.DO_NOT_QUEUE) != RequestNameReply.PRIMARY_OWNER:
            power_bus.disconnect()
            bus.disconnect()
            raise RuntimeError("Romulus legacy power service name already owned")
    try:
        while True:
            try:
                values, power = await asyncio.to_thread(readings, args.origin, args.token_file, args.ca_file)
            except (OSError, HTTPException, ValueError, KeyError, IndexError, TypeError):
                values, power = {}, None
            for name, sensor in sensors.items():
                sensor.update(values.get(name, math.nan))
            if romulus is not None:
                romulus.update(power)
            for index, (_, state, availability) in enumerate(states):
                if power is not None:
                    state.update(power[index])
                availability.update(power is not None)
            await asyncio.sleep(args.interval)
    finally:
        for state_bus in state_buses:
            state_bus.disconnect()
        bus.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--operator-token-file", type=Path,
                        help="enable host transitions on a trusted private D-Bus")
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--bus-address", required=True)
    parser.add_argument("--sensors-only", action="store_true", help="leave host and chassis state to upstream services")
    parser.add_argument("--external-mapper", action="store_true",
                        help="let the bus's ObjectMapper discover and publish associations")
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--upstream-power", action="store_true",
                        help="own upstream host and chassis state names in a dedicated test guest")
    parser.add_argument("--romulus-power", action="store_true",
                        help="provide the legacy power driver to native Romulus state services")
    args = parser.parse_args()
    if args.upstream_power and (not args.external_mapper or args.sensors_only or not args.operator_token_file):
        parser.error("--upstream-power requires --external-mapper and an operator token")
    if args.romulus_power and (not args.external_mapper or not args.sensors_only or not args.operator_token_file):
        parser.error("--romulus-power requires --external-mapper, --sensors-only and an operator token")
    if not 0 < args.interval < float("inf"):
        parser.error("interval must be finite and positive")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()

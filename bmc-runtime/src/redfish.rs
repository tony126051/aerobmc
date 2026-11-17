use crate::{lock, submit_action, Controller, EventKind, Input, Inputs, Power};
use crate::events::Event;
use serde_json::{json, Value};
use std::string::{String, ToString};
use std::sync::Mutex;
use std::vec::Vec;

const SYSTEM: &str = "/redfish/v1/Systems/Host";
const RESET: &str = "/redfish/v1/Systems/Host/Actions/ComputerSystem.Reset";
const CHASSIS: &str = "/redfish/v1/Chassis/G1";
const SENSORS: &str = "/redfish/v1/Chassis/G1/Sensors";
const TASKS: &str = "/redfish/v1/TaskService/Tasks";
const LOGS: &str = "/redfish/v1/Systems/Host/LogServices";
const LOG: &str = "/redfish/v1/Systems/Host/LogServices/Runtime";
const ENTRIES: &str = "/redfish/v1/Systems/Host/LogServices/Runtime/Entries";
const SENSOR_PATHS: [&str; 2] = [
    "/redfish/v1/Chassis/G1/Sensors/inlet", "/redfish/v1/Chassis/G1/Sensors/board",
];
const ODATA: &str = "OData-Version: 4.0\r\n";

pub fn expects_body(head: &str) -> bool {
    let mut fields = head.lines().next().unwrap_or("").split_ascii_whitespace();
    fields.next() == Some("POST") && fields.next() == Some(RESET)
}

pub fn body_length(head: &str) -> Option<usize> {
    let mut length = None;
    for header in head.split("\r\n").skip(1) {
        let (name, value) = header.split_once(':')?;
        if name.trim() != name || name.eq_ignore_ascii_case("Transfer-Encoding") {
            return None;
        }
        if name.eq_ignore_ascii_case("Content-Length") {
            if length.is_some() {
                return None;
            }
            length = Some(value.trim().parse().ok()?);
        }
    }
    length.filter(|length| (1..=256).contains(length))
}

fn link(path: &str) -> Value {
    json!({"@odata.id": path})
}

fn collection(path: &str, kind: &str, members: &[&str]) -> Value {
    json!({
        "@odata.id": path, "@odata.type": format!("#{kind}Collection.{kind}Collection"),
        "Name": format!("{kind} Collection"), "Members@odata.count": members.len(),
        "Members": members.iter().map(|path| link(path)).collect::<Vec<_>>(),
    })
}

fn error(code: &'static str, message: &str) -> (&'static str, String, String) {
    (code, ODATA.into(), json!({
        "error": {"code": "Base.1.0.GeneralError", "message": message}
    }).to_string())
}

fn task(id: u64, inputs: &Inputs) -> Option<Value> {
    let outcome = inputs.completed.iter().find(|result| result.ticket.id == id);
    let (state, status) = match outcome {
        Some(result) => match result.event {
            Some(EventKind::ThermalStartBlocked | EventKind::FanResetRejected) => ("Exception", "Critical"),
            _ => ("Completed", "OK"),
        },
        None if inputs.pending.iter().any(|(_, ticket)| ticket.as_ref().is_some_and(|ticket| ticket.id == id)) =>
            ("Pending", "OK"),
        None => return None,
    };
    Some(json!({
        "@odata.id": format!("{TASKS}/{id}"), "@odata.type": "#Task.v1_4_0.Task",
        "Id": id.to_string(), "Name": "Management Action", "TaskState": state, "TaskStatus": status,
        "Oem": {"BmcG1": {"Event": outcome.and_then(|result| result.event).map(EventKind::name)}}
    }))
}

fn event_entry(event: &Event) -> Value {
    let severity = match event.kind {
        EventKind::PowerLocked | EventKind::WatchdogTripped | EventKind::PowerGoodLost
        | EventKind::SensorCritical(_) | EventKind::ThermalTripped(_)
        | EventKind::ThermalTelemetryLost => "Critical",
        EventKind::PowerTimedOut | EventKind::FanProtection | EventKind::FanResetRejected
        | EventKind::SensorHigh(_) | EventKind::SensorStale(_) | EventKind::ThermalStartBlocked => "Warning",
        _ => "OK",
    };
    let mut value = json!({
        "@odata.id": format!("{ENTRIES}/{}", event.sequence),
        "@odata.type": "#LogEntry.v1_9_0.LogEntry",
        "Id": event.sequence.to_string(), "Name": "Runtime Event",
        "EntryType": "Oem", "OemRecordFormat": "BMC-G1 Runtime",
        "Severity": severity, "Message": event.kind.name(),
        "Oem": {"BmcG1": {"MonotonicMilliseconds": event.at_ms, "Revision": event.revision}},
    });
    if let Some(sensor) = event.kind.sensor_id().and_then(|id| SENSOR_PATHS.get(usize::from(id))) {
        value["Links"] = json!({"OriginOfCondition": link(sensor)});
    }
    value
}

pub fn route(method: &str, path: &str, request: &str, snapshot: &Mutex<Controller>,
             inputs: &Mutex<Inputs>) -> (&'static str, String, String) {
    if method == "POST" && path == RESET {
        let Some((head, body)) = request.split_once("\r\n\r\n") else {
            return error("400 Bad Request", "Missing request body");
        };
        if body_length(head) != Some(body.len()) {
            return error("400 Bad Request", "Invalid request length");
        }
        let Ok(params) = serde_json::from_str::<Value>(body) else {
            return error("400 Bad Request", "Invalid JSON");
        };
        let input = match params.as_object().filter(|object| object.len() == 1)
            .and_then(|object| object.get("ResetType")).and_then(Value::as_str) {
            Some("On") => Input::Start,
            Some("ForceOff") => Input::Stop,
            Some("PowerCycle") => Input::Cycle,
            _ => return error("400 Bad Request", "Unsupported ResetType or parameters"),
        };
        return match submit_action(head, input, inputs) {
            Ok(id) => (
                "202 Accepted", format!("{ODATA}Location: /redfish/v1/TaskMonitor/{id}\r\n"),
                task(id, &lock(inputs)).unwrap_or_else(|| link(&format!("{TASKS}/{id}"))).to_string(),
            ),
            Err((code, reason)) => error(code, &reason),
        };
    }
    if method != "GET" {
        return error("405 Method Not Allowed", "Method not allowed");
    }
    let value = match path {
        "/redfish/v1" | "/redfish/v1/" => json!({
            "@odata.id": "/redfish/v1/", "@odata.type": "#ServiceRoot.v1_0_0.ServiceRoot",
            "Id": "RootService", "Name": "BMC-G1", "RedfishVersion": "1.0.0",
            "Systems": link("/redfish/v1/Systems"), "Chassis": link("/redfish/v1/Chassis"),
            "TaskService": link("/redfish/v1/TaskService"),
        }),
        "/redfish/v1/Systems" => collection(path, "ComputerSystem", &[SYSTEM]),
        "/redfish/v1/Chassis" => collection(path, "Chassis", &[CHASSIS]),
        SYSTEM => {
            let state = *lock(snapshot);
            let power = match state.power {
                Power::On => "On",
                Power::Starting { .. } => "PoweringOn",
                _ => "Off",
            };
            let fault = match state.power {
                Power::Failed => Some("power_locked"),
                Power::WatchdogTripped => Some("watchdog_tripped"),
                Power::PowerGoodLost => Some("power_good_lost"),
                Power::ThermalTripped => Some("thermal_tripped"),
                Power::ThermalTelemetryLost => Some("thermal_telemetry_lost"),
                _ => None,
            };
            let health = if fault.is_some() || state.sensors.iter().any(|sensor| sensor.critical) {
                "Critical"
            } else if state.fan_protected || state.sensors.iter().any(|sensor| sensor.high || sensor.stale) {
                "Warning"
            } else {
                "OK"
            };
            json!({
                "@odata.id": path, "@odata.type": "#ComputerSystem.v1_17_0.ComputerSystem",
                "Id": "Host", "Name": "Simulated Host", "SystemType": "Physical", "PowerState": power,
                "Links": {"Chassis": [link(CHASSIS)]}, "LogServices": link(LOGS),
                "Status": {"Health": health},
                "Oem": {"BmcG1": {"PowerFault": fault, "Revision": state.revision}},
                "Actions": {"#ComputerSystem.Reset": {
                    "target": RESET, "ResetType@Redfish.AllowableValues": ["On", "ForceOff", "PowerCycle"]
                }},
            })
        }
        LOGS => collection(path, "LogService", &[LOG]),
        LOG => json!({
            "@odata.id": path, "@odata.type": "#LogService.v1_2_0.LogService",
            "Id": "Runtime", "Name": "Runtime Event Log", "ServiceEnabled": true,
            "OverWritePolicy": "WrapsWhenFull", "Entries": link(ENTRIES),
            "MaxNumberOfRecords": crate::events::CAPACITY,
            "Oem": {"BmcG1": {"Overwritten": lock(snapshot).events.overwritten()}},
        }),
        ENTRIES => {
            let events = lock(snapshot).events;
            let members: Vec<_> = events.iter().map(|event| format!("{ENTRIES}/{}", event.sequence)).collect();
            let members: Vec<_> = members.iter().map(String::as_str).collect();
            collection(path, "LogEntry", &members)
        }
        CHASSIS => json!({
            "@odata.id": path, "@odata.type": "#Chassis.v1_15_0.Chassis",
            "Id": "G1", "Name": "Simulated Chassis", "ChassisType": "Other",
            "Sensors": link(SENSORS), "Links": {"ComputerSystems": [link(SYSTEM)]},
        }),
        SENSORS => collection(path, "Sensor", &[
            "/redfish/v1/Chassis/G1/Sensors/inlet", "/redfish/v1/Chassis/G1/Sensors/board",
        ]),
        "/redfish/v1/Chassis/G1/Sensors/inlet" | "/redfish/v1/Chassis/G1/Sensors/board" => {
            let sensor = lock(snapshot).sensors[usize::from(path.ends_with("/board"))];
            json!({
                "@odata.id": path, "@odata.type": "#Sensor.v1_2_0.Sensor",
                "Id": sensor.name, "Name": sensor.name,
                "ReadingType": "Temperature", "ReadingUnits": "Cel",
                "Reading": sensor.value.map(|value| f64::from(value) / 1000.0),
                "Status": {
                    "State": if sensor.value.is_some() { "Enabled" } else { "UnavailableOffline" },
                    "Health": if sensor.critical { "Critical" } else if sensor.high || sensor.stale { "Warning" } else { "OK" }
                }
            })
        }
        "/redfish/v1/TaskService" => json!({
            "@odata.id": path, "@odata.type": "#TaskService.v1_0_0.TaskService",
            "Id": "TaskService", "Name": "Task Service", "ServiceEnabled": true,
            "Tasks": link(TASKS),
        }),
        TASKS => {
            let inputs = lock(inputs);
            let members: Vec<_> = inputs.completed.iter().map(|result| result.ticket.id)
                .chain(inputs.pending.iter().filter_map(|(_, ticket)| ticket.as_ref().map(|ticket| ticket.id)))
                .map(|id| format!("{TASKS}/{id}")).collect();
            let members: Vec<_> = members.iter().map(String::as_str).collect();
            collection(path, "Task", &members)
        }
        _ if path.starts_with("/redfish/v1/Systems/Host/LogServices/Runtime/Entries/") => {
            let sequence = path.rsplit('/').next().and_then(|id| id.parse::<u64>().ok());
            let events = lock(snapshot).events;
            let Some(event) = events.iter().find(|event| Some(event.sequence) == sequence) else {
                return error("404 Not Found", "Log entry not retained");
            };
            event_entry(event)
        }
        _ => {
            let monitor = path.starts_with("/redfish/v1/TaskMonitor/");
            let id = path.strip_prefix("/redfish/v1/TaskMonitor/")
                .or_else(|| path.strip_prefix("/redfish/v1/TaskService/Tasks/"))
                .and_then(|id| id.parse().ok());
            let Some(value) = id.and_then(|id| task(id, &lock(inputs))) else {
                return error("404 Not Found", "Resource not found");
            };
            if monitor {
                return match value["TaskState"].as_str() {
                    Some("Pending") => ("202 Accepted", format!("{ODATA}Location: {path}\r\n"), value.to_string()),
                    Some("Exception") => error("409 Conflict", "Action rejected by the controller"),
                    _ => ("204 No Content", ODATA.into(), String::new()),
                };
            }
            value
        }
    };
    ("200 OK", ODATA.into(), value.to_string())
}

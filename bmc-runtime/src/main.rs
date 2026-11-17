#![cfg_attr(target_os = "none", no_std)]
#![cfg_attr(target_os = "none", no_main)]

#[cfg(target_os = "none")]
#[macro_use]
extern crate axstd as std;

mod controller;
mod events;
mod sensor;
mod redfish;
mod access;
use access::Access;
#[cfg(any(target_os = "none", all(target_os = "linux", target_arch = "aarch64")))]
mod gpio;

use events::EventKind;
use controller::{Controller, Input, Power, POWER_ATTEMPT_LIMIT, WATCHDOG_TIMEOUT_MS};
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use core::ops::DerefMut;
use core::sync::atomic::{AtomicUsize, Ordering};
use std::collections::VecDeque;
use std::string::{String, ToString};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use std::vec::Vec;

const INPUT_CAPACITY: usize = 16;

fn lock<T>(mutex: &Mutex<T>) -> impl DerefMut<Target = T> + '_ {
    #[cfg(target_os = "none")]
    return mutex.lock();
    #[cfg(not(target_os = "none"))]
    return mutex.lock().unwrap();
}

fn millis() -> u64 {
    #[cfg(target_os = "none")]
    return arceos_api::time::ax_monotonic_time().as_millis() as u64;
    #[cfg(all(target_os = "linux", target_arch = "aarch64"))]
    return gpio::monotonic_ms();
    #[cfg(not(any(target_os = "none", all(target_os = "linux", target_arch = "aarch64"))))]
    {
        use std::{sync::OnceLock, time::Instant};
        static START: OnceLock<Instant> = OnceLock::new();
        START.get_or_init(Instant::now).elapsed().as_millis() as u64
    }
}

fn management_command(name: &str) -> Option<Input> {
    match name {
        "start" => Some(Input::Start),
        "stop" => Some(Input::Stop),
        "cycle" => Some(Input::Cycle),
        "fan-reset" => Some(Input::FanReset),
        _ => None,
    }
}

const ACTION_HISTORY_CAPACITY: usize = 32;

struct ActionTicket {
    id: u64,
    key: Option<String>,
}

struct ActionResult {
    ticket: ActionTicket,
    input: Input,
    event: Option<EventKind>,
    revision: u64,
}

struct Inputs {
    pending: VecDeque<(Input, Option<ActionTicket>)>,
    completed: VecDeque<ActionResult>,
    last_id: u64,
}

impl Inputs {
    fn new() -> Self {
        Self {
            pending: VecDeque::with_capacity(INPUT_CAPACITY),
            completed: VecDeque::with_capacity(ACTION_HISTORY_CAPACITY),
            last_id: 0,
        }
    }

    fn enqueue(&mut self, input: Input, tracked: bool, key: Option<&str>) -> Option<u64> {
        if self.pending.len() >= INPUT_CAPACITY {
            return None;
        }
        let id = if tracked {
            self.last_id = self.last_id.checked_add(1)?;
            Some(self.last_id)
        } else {
            None
        };
        self.pending.push_back((input, id.map(|id| ActionTicket { id, key: key.map(str::to_string) })));
        Some(id.unwrap_or(0))
    }

    fn find_key(&self, key: &str) -> Option<(Input, u64)> {
        self.pending.iter().filter_map(|(input, ticket)| {
            ticket.as_ref().map(|ticket| (*input, ticket))
        }).chain(self.completed.iter().map(|result| (result.input, &result.ticket)))
            .find(|(_, ticket)| ticket.key.as_deref() == Some(key))
            .map(|(input, ticket)| (input, ticket.id))
    }

    fn complete(&mut self, result: ActionResult) {
        if self.completed.len() == ACTION_HISTORY_CAPACITY {
            self.completed.pop_front();
        }
        self.completed.push_back(result);
    }

    fn receipt(&self, id: u64) -> (&'static str, String) {
        if let Some(result) = self.completed.iter().find(|result| result.ticket.id == id) {
            let outcome = match result.event {
                Some(EventKind::ThermalStartBlocked | EventKind::FanResetRejected) => "rejected",
                Some(_) => "applied",
                None => "no_op",
            };
            let event = result.event.map_or_else(|| "null".into(), |kind| format!("\"{}\"", kind.name()));
            return ("200 OK", format!(
                "{{\"id\":{id},\"state\":\"{outcome}\",\"revision\":{},\"event\":{event}}}",
                result.revision,
            ));
        }
        if self.pending.iter().any(|(_, ticket)| ticket.as_ref().is_some_and(|ticket| ticket.id == id)) {
            return ("200 OK", format!("{{\"id\":{id},\"state\":\"pending\"}}"));
        }
        ("404 Not Found", "{\"error\":\"not_found\"}".into())
    }
}

fn parse_input(line: &str) -> Option<Input> {
    let line = line.trim();
    if let Some(input) = management_command(line) {
        return Some(input);
    }
    match line {
        "power-good" => Some(Input::PowerGood),
        line => {
            let (name, value) = line.split_once(' ')?;
            match name {
                "temperature" => {
                    let (sensor, value) = match value.split_once(' ') {
                        Some((sensor, value)) => (sensor.parse().ok()?, value),
                        None => (0, value),
                    };
                    Some(Input::Temperature { sensor, value: value.parse().ok()? })
                }
                "tach" => Some(Input::Tach(value.parse().ok()?)),
                _ => None,
            }
        }
    }
}

fn optional_number<T: ToString>(value: Option<T>) -> String {
    value.map_or_else(|| "null".into(), |number| number.to_string())
}

fn status(state: Controller) -> String {
    let (power, enabled) = match state.power {
        Power::Off => ("off", false),
        Power::Starting { .. } => ("starting", true),
        Power::On => ("on", true),
        Power::Failed => ("failed", false),
        Power::WatchdogTripped => ("watchdog_tripped", false),
        Power::PowerGoodLost => ("power_good_lost", false),
        Power::ThermalTripped => ("thermal_tripped", false),
        Power::ThermalTelemetryLost => ("thermal_telemetry_lost", false),
        Power::RetryWait { .. } => ("retry_wait", false),
        Power::CycleWait { .. } => ("cycle_wait", false),
    };
    use core::fmt::Write as _;
    let mut sensors = String::new();
    for (id, sensor) in state.sensors.iter().enumerate() {
        if id > 0 {
            sensors.push(',');
        }
        write!(sensors, "{{\"id\":{id},\"name\":\"{}\",\"temperature_millicelsius\":{},\"state\":\"{}\",\"critical\":{}}}",
            sensor.name, optional_number(sensor.value), sensor.state(), sensor.critical).unwrap();
    }
    format!(
        concat!(
            "{{\"revision\":{},\"power_good_edges\":{},\"sensor\":{{\"temperature_millicelsius\":{}}},",
            "\"sensors\":[{}],\"power\":{{\"state\":\"{}\",\"enabled\":{}}},",
            "\"power_attempt\":{},\"power_attempt_limit\":{},",
            "\"watchdog\":{{\"armed\":{},\"heartbeat_edges\":{},\"trips\":{},\"timeout_ms\":{}}},",
            "\"fan\":{{\"rpm\":{},\"protected\":{},\"duty_percent\":{}}}}}"
        ),
        state.revision,
        state.power_good_edges,
        optional_number(state.sensors[0].value),
        sensors,
        power,
        enabled,
        state.power_attempt,
        POWER_ATTEMPT_LIMIT,
        state.watchdog_last_ms.is_some(),
        state.heartbeat_edges,
        state.watchdog_trips,
        WATCHDOG_TIMEOUT_MS,
        optional_number(state.rpm),
        state.fan_protected,
        state.fan_duty(),
    )
}

fn event_log(state: Controller, stream_id: Option<&str>) -> String {
    use core::fmt::Write as _;
    let mut body = format!(
        "{{\"revision\":{},\"overwritten\":{}",
        state.revision, state.events.overwritten(),
    );
    if let Some(id) = stream_id {
        write!(body, ",\"stream_id\":\"{id}\",\"control_progress\":{}", state.control_progress).expect("write stream identity");
    }
    body.push_str(",\"events\":[");
    for (index, event) in state.events.iter().enumerate() {
        if index != 0 {
            body.push(',');
        }
        write!(
            body,
            "{{\"sequence\":{},\"at_ms\":{},\"revision\":{},\"kind\":\"{}\",\"sensor_id\":{}}}",
            event.sequence, event.at_ms, event.revision, event.kind.name(), optional_number(event.kind.sensor_id()),
        ).expect("write JSON event");
    }
    body.push_str("]}");
    body
}

fn serve(mut stream: TcpStream, snapshot: &Mutex<Controller>, inputs: &Mutex<Inputs>, access: &Mutex<Access>) -> io::Result<()> {
    #[cfg(not(target_os = "none"))]
    {
        stream.set_read_timeout(Some(Duration::from_secs(1)))?;
        stream.set_write_timeout(Some(Duration::from_secs(1)))?;
    }
    for index in 0..256 {
        let mut request = [0; 2048];
        let mut used = 0;
        let complete = loop {
            if let Some(end) = request[..used].windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                let head = core::str::from_utf8(&request[..end]).unwrap_or("");
                if !redfish::expects_body(head) {
                    break true;
                }
                let Some(length) = redfish::body_length(head) else {
                    break true;
                };
                if used >= end + 4 + length {
                    break true;
                }
            }
            if used == request.len() {
                break false;
            }
            let count = stream.read(&mut request[used..])?;
            if count == 0 {
                return Ok(());
            }
            used += count;
        };
        let request = std::str::from_utf8(&request[..used]).unwrap_or("");
        let line = request.lines().next().unwrap_or("");
        let fields: Vec<_> = line.split_ascii_whitespace().collect();
        // Keep policy changes and HTTP dispatch ordered, including enqueue.
        let policy = lock(access);
        let authorization = policy.authorize(request, fields.first() == Some(&"GET"));
        let (code, headers, body) = match (authorization, complete, fields.as_slice()) {
            (Err(401), _, _) => ("401 Unauthorized", "WWW-Authenticate: Bearer\r\n".into(),
                                  "{\"error\":\"unauthorized\"}".into()),
            (Err(_), _, _) => ("403 Forbidden", String::new(), "{\"error\":\"forbidden\"}".into()),
            (Ok(()), true, [method, path, "HTTP/1.0" | "HTTP/1.1"]) if path.starts_with("/redfish/") => {
                redfish::route(method, path, request, snapshot, inputs)
            }
            (Ok(()), true, ["GET", "/v1/status", "HTTP/1.0" | "HTTP/1.1"]) => (
                "200 OK", String::new(), status(*lock(snapshot)),
            ),
            (Ok(()), true, ["GET", "/v1/events", "HTTP/1.0" | "HTTP/1.1"]) => (
                "200 OK", String::new(), event_log(*lock(snapshot), policy.stream_id.as_deref()),
            ),
            (Ok(()), true, ["GET", path, "HTTP/1.0" | "HTTP/1.1"]) if path.starts_with("/v1/actions/") => {
                let (code, body) = path["/v1/actions/".len()..].parse::<u64>()
                    .map_or_else(|_| ("404 Not Found", "{\"error\":\"not_found\"}".into()),
                                 |id| lock(inputs).receipt(id));
                (code, String::new(), body)
            }
            (Ok(()), true, ["POST", path, "HTTP/1.0" | "HTTP/1.1"]) if path.starts_with("/v1/actions/") => {
                let (code, body) = action(request, &path["/v1/actions/".len()..], inputs);
                (code, String::new(), body)
            }
            (Ok(()), true, ["GET", _, "HTTP/1.0" | "HTTP/1.1"]) => (
                "404 Not Found", String::new(), "{\"error\":\"not_found\"}".into(),
            ),
            (Ok(()), true, [_, _, "HTTP/1.0" | "HTTP/1.1"]) => (
                "405 Method Not Allowed", "Allow: GET\r\n".into(), "{\"error\":\"read_only\"}".into(),
            ),
            _ => ("400 Bad Request", String::new(), "{\"error\":\"bad_request\"}".into()),
        };
        drop(policy);
        let keep_alive = index + 1 < 256 && code == "200 OK"
            && matches!(fields.as_slice(), ["GET", _, "HTTP/1.1"])
            && reusable_request(request);
        let connection = if keep_alive { "keep-alive" } else { "close" };
        write!(
            stream,
            "HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n{headers}Connection: {connection}\r\n\r\n{body}",
            body.len()
        )?;
        if !keep_alive {
            return Ok(());
        }
    }
    Ok(())
}

fn reusable_request(request: &str) -> bool {
    let Some((headers, "")) = request.split_once("\r\n\r\n") else {
        return false;
    };
    let mut keep_alive = false;
    for header in headers.split("\r\n").skip(1) {
        let Some((name, value)) = header.split_once(':') else {
            return false;
        };
        if name.trim() != name || name.eq_ignore_ascii_case("Transfer-Encoding")
            || (name.eq_ignore_ascii_case("Content-Length") && value.trim() != "0")
        {
            return false;
        }
        if name.eq_ignore_ascii_case("Connection") {
            if !value.trim().eq_ignore_ascii_case("keep-alive") {
                return false;
            }
            keep_alive = true;
        }
    }
    keep_alive
}

fn action(request: &str, name: &str, inputs: &Mutex<Inputs>) -> (&'static str, String) {
    let Some(input) = management_command(name) else {
        return ("404 Not Found", "{\"error\":\"not_found\"}".into());
    };
    let Some((headers, "")) = request.split_once("\r\n\r\n") else {
        return ("400 Bad Request", "{\"error\":\"bad_request\"}".into());
    };
    if headers.split("\r\n").skip(1).any(|header| {
        header.split_once(':').is_some_and(|(name, value)|
            name.eq_ignore_ascii_case("Content-Length") && value.trim() != "0")
    }) {
        return ("400 Bad Request", "{\"error\":\"bad_request\"}".into());
    }
    match submit_action(headers, input, inputs) {
        Ok(id) => ("202 Accepted", format!("{{\"queued\":true,\"id\":{id}}}")),
        Err(error) => error,
    }
}

fn submit_action(headers: &str, input: Input, inputs: &Mutex<Inputs>) -> Result<u64, (&'static str, String)> {
    let mut confirmed = false;
    let mut length_seen = false;
    let mut key = None;
    for header in headers.split("\r\n").skip(1) {
        let Some((name, value)) = header.split_once(':') else {
            return Err(("400 Bad Request", "{\"error\":\"bad_request\"}".into()));
        };
        if name.trim() != name || name.eq_ignore_ascii_case("Transfer-Encoding") {
            return Err(("400 Bad Request", "{\"error\":\"bad_request\"}".into()));
        }
        if name.eq_ignore_ascii_case("Content-Length") {
            if length_seen || value.trim().parse::<usize>().is_err() {
                return Err(("400 Bad Request", "{\"error\":\"bad_request\"}".into()));
            }
            length_seen = true;
        }
        if name.eq_ignore_ascii_case("Idempotency-Key") {
            let value = value.trim();
            if key.is_some() || value.is_empty() || value.len() > 64
                || !value.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_.".contains(&byte))
            {
                return Err(("400 Bad Request", "{\"error\":\"invalid_idempotency_key\"}".into()));
            }
            key = Some(value);
        }
        if name.eq_ignore_ascii_case("X-BMC-Action") {
            confirmed = value.trim() == "1";
        }
    }
    if !confirmed {
        return Err(("403 Forbidden", "{\"error\":\"action_header_required\"}".into()));
    }
    let mut inputs = lock(inputs);
    if let Some((original, id)) = key.and_then(|key| inputs.find_key(key)) {
        if original != input {
            return Err(("409 Conflict", "{\"error\":\"idempotency_key_conflict\"}".into()));
        }
        return Ok(id);
    }
    if let Some(id) = inputs.enqueue(input, true, key) {
        Ok(id)
    } else {
        Err(("503 Service Unavailable", "{\"error\":\"queue_full\"}".into()))
    }
}

fn run() -> io::Result<()> {
    // On QEMU virt the platform's default UART ISR consumes RX bytes to echo them.
    // Give the fixture reader exclusive access to UART RX.
    #[cfg(target_os = "none")]
    axhal::irq::set_enable(axconfig::devices::UART_IRQ, false);
    #[cfg(target_os = "none")]
    let (address, guest) = (("0.0.0.0", 5555), true);
    #[cfg(not(target_os = "none"))]
    let (address, guest) = {
        let mut args = std::env::args().skip(1);
        let port = args.next().map_or(Ok(5555u16), |arg| arg.parse())
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let guest = args.next().as_deref() == Some("--guest");
        let bind = if guest { "0.0.0.0" } else { "127.0.0.1" };
        ((bind, port), guest)
    };
    #[cfg(target_os = "none")]
    let mut device = Some(gpio::Device::open());
    #[cfg(all(target_os = "linux", target_arch = "aarch64"))]
    let mut device = if guest { Some(gpio::Device::open()?) } else { None };
    let access = Arc::new(Mutex::new(Access::new()));
    let serial_access = Arc::clone(&access);
    let listener = TcpListener::bind(address)?;
    let snapshot = Arc::new(Mutex::new(Controller::new()));
    let inputs = Arc::new(Mutex::new(Inputs::new()));
    let control_snapshot = Arc::clone(&snapshot);
    let control_inputs = Arc::clone(&inputs);
    let serial_inputs = Arc::clone(&inputs);
    let start = millis();
    let frozen = Arc::new(core::sync::atomic::AtomicBool::new(false));
    let serial_frozen = Arc::clone(&frozen);
    std::thread::spawn(move || {
        let mut state = Controller::new();
        #[cfg(any(target_os = "none", all(target_os = "linux", target_arch = "aarch64")))]
        let mut last_outputs = None;
        loop {
            if frozen.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(5));
                continue;
            }
            #[cfg(any(target_os = "none", all(target_os = "linux", target_arch = "aarch64")))]
            if let Some(device) = device.as_mut() {
                device.drain(&mut state, start);
            }
            let now = millis().saturating_sub(start);
            let mut pending = lock(&control_inputs);
            let mut receipt = None;
            while let Some((input, ticket)) = pending.pending.pop_front() {
                let before = state.power;
                let event = state.apply(now, input);
                if let Some(ticket) = ticket {
                    receipt = Some(ActionResult { ticket, input, event, revision: state.revision });
                    break;
                }
                if before != state.power && matches!(state.power,
                    Power::Off | Power::ThermalTripped | Power::ThermalTelemetryLost)
                {
                    // Publish the power-off output before processing further commands.
                    break;
                }
            }
            state.tick(now);
            #[cfg(any(target_os = "none", all(target_os = "linux", target_arch = "aarch64")))]
            if let Some(device) = device.as_ref() {
                let outputs = (state.power, state.fan_duty());
                if last_outputs != Some(outputs) {
                    device.publish(&state);
                    last_outputs = Some(outputs);
                }
            }
            if matches!(state.power, Power::CycleWait { until_ms: None }) {
                state.outputs_published(millis().saturating_sub(start));
            }
            state.control_progress += 1;
            *lock(&control_snapshot) = state;
            if let Some(receipt) = receipt {
                pending.complete(receipt);
            }
            drop(pending);
            std::thread::sleep(Duration::from_millis(5));
        }
    });
    std::thread::spawn(move || {
        #[cfg(not(target_os = "none"))]
        let mut stdin = io::stdin();
        let mut line = [0; 192];
        let mut used = 0;
        let mut overflow = false;
        let mut byte = [0];
        loop {
            #[cfg(target_os = "none")]
            match arceos_api::stdio::ax_console_read_bytes(&mut byte) {
                Ok(1) => {}
                Ok(0) => {
                    std::thread::sleep(Duration::from_millis(1));
                    continue;
                }
                _ => break,
            }
            #[cfg(not(target_os = "none"))]
            if !matches!(stdin.read(&mut byte), Ok(1)) {
                break;
            }
            if byte[0] == b'\n' {
                if !overflow {
                    let command = core::str::from_utf8(&line[..used]).unwrap_or("");
                    if option_env!("BMC_TEST_CONTROL_STALL") == Some("1") && command == "test-control-stall" {
                        serial_frozen.store(true, Ordering::Relaxed);
                    }
                    if let Some(tokens) = command.trim().strip_prefix("auth-enable ") {
                        let mut fields = tokens.split_ascii_whitespace();
                        let enabled = match (fields.next(), fields.next(), fields.next(), fields.next()) {
                            (Some(reader), Some(operator), stream_id, None) =>
                                lock(&serial_access).provision(reader, operator, stream_id),
                            _ => false,
                        };
                        if enabled {
                            println!("BMC_AUTH_ENABLED");
                        } else {
                            println!("BMC_AUTH_REJECTED");
                        }
                    }
                    if let Some(input) = parse_input(command) {
                        if guest && matches!(input, Input::PowerGood | Input::Tach(_)) {
                            used = 0;
                            continue;
                        }
                        if lock(&serial_inputs).enqueue(input, false, None).is_none() {
                            println!("BMC_INPUT_QUEUE_FULL");
                        }
                    }
                }
                used = 0;
                overflow = false;
            } else if used < line.len() {
                line[used] = byte[0];
                used += 1;
            } else {
                overflow = true;
            }
        }
    });

    println!("{}", listener.local_addr()?);
    println!("BMC_RUNTIME_READY");
    let active = Arc::new(AtomicUsize::new(0));
    loop {
        let (stream, _) = match listener.accept() {
            #[cfg(not(target_os = "none"))]
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => continue,
            #[cfg(target_os = "none")]
            Err(io::Error::WouldBlock) => continue,
            result => result?,
        };
        if active.load(Ordering::Acquire) >= 8 {
            drop(stream);
            continue;
        }
        active.fetch_add(1, Ordering::Relaxed);
        let workers = Arc::clone(&active);
        let snapshot = Arc::clone(&snapshot);
        let inputs = Arc::clone(&inputs);
        let access = Arc::clone(&access);
        std::thread::spawn(move || {
            let _ = serve(stream, &snapshot, &inputs, &access);
            workers.fetch_sub(1, Ordering::Release);
        });
    }
}

#[cfg_attr(target_os = "none", unsafe(no_mangle))]
fn main() {
    run().expect("BMC runtime failed");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_and_completed_receipts_have_bounded_history() {
        let mut inputs = Inputs::new();
        for revision in 1..=33 {
            let id = inputs.enqueue(Input::Stop, true, None).unwrap();
            assert_eq!(inputs.receipt(id).1, format!("{{\"id\":{id},\"state\":\"pending\"}}"));
            let (input, ticket) = inputs.pending.pop_front().unwrap();
            inputs.complete(ActionResult { ticket: ticket.unwrap(), input, event: None, revision });
        }
        assert_eq!(inputs.receipt(1).0, "404 Not Found");
        assert_eq!(inputs.receipt(2).0, "200 OK");
        assert_eq!(inputs.receipt(33).1, "{\"id\":33,\"state\":\"no_op\",\"revision\":33,\"event\":null}");
    }

    #[test]
    fn action_replay_survives_completion_and_bypasses_full_queue() {
        let queue = Mutex::new(Inputs::new());
        let request = "POST /v1/actions/stop HTTP/1.1\r\nX-BMC-Action: 1\r\nIdempotency-Key: retry-1\r\n\r\n";
        let original = action(request, "stop", &queue);
        assert_eq!(original.0, "202 Accepted");
        assert_eq!(action(request, "stop", &queue), original);
        assert_eq!(action(request, "cycle", &queue).0, "409 Conflict");
        {
            let mut inputs = lock(&queue);
            assert_eq!(inputs.pending.len(), 1);
            let (input, ticket) = inputs.pending.pop_front().unwrap();
            inputs.complete(ActionResult { ticket: ticket.unwrap(), input, event: None, revision: 0 });
            for _ in 0..INPUT_CAPACITY {
                inputs.enqueue(Input::Start, false, None).unwrap();
            }
        }
        assert_eq!(action(request, "stop", &queue), original);
        assert_eq!(action(&request.replace("retry-1", "invalid key"), "stop", &queue).0, "400 Bad Request");
        assert_eq!(action(&request.replace("retry-1", "retry-2"), "stop", &queue).0, "503 Service Unavailable");
    }

    #[test]
    fn management_actions_reject_ambiguous_framing_and_full_queue() {
        let queue = Mutex::new(Inputs::new());
        let base = "POST /v1/actions/stop HTTP/1.1\r\nX-BMC-Action: 1\r\n";
        for headers in ["Content-Length: 0\r\nContent-Length: 0\r\n", "Transfer-Encoding: chunked\r\n"] {
            assert_eq!(action(&format!("{base}{headers}\r\n"), "stop", &queue).0, "400 Bad Request");
        }
        assert!(lock(&queue).pending.is_empty());
        let request = format!("{base}Content-Length: 0\r\n\r\n");
        for _ in 0..INPUT_CAPACITY {
            assert_eq!(action(&request, "stop", &queue).0, "202 Accepted");
        }
        assert_eq!(action(&request, "stop", &queue).0, "503 Service Unavailable");
        assert_eq!(lock(&queue).pending.len(), INPUT_CAPACITY);
    }
}

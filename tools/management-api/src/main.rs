#![cfg_attr(target_os = "none", no_std)]
#![cfg_attr(target_os = "none", no_main)]

#[cfg(target_os = "none")]
#[macro_use]
extern crate axstd as std;

use axbmc_core::fan_model::{self, Pwm};
use axbmc_core::power::{Feedback, Power, PowerState};
use axbmc_core::{EventQueue, Sensor, State};
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};

const TACH_LOSS_NS: u64 = 5_000_000_000;
const PORT: u16 = 5555;

fn clock() -> u64 {
    #[cfg(target_os = "none")]
    {
        arceos_api::time::ax_monotonic_time().as_nanos() as u64
    }
    #[cfg(not(target_os = "none"))]
    {
        use std::{sync::OnceLock, time::Instant};
        static START: OnceLock<Instant> = OnceLock::new();
        START.get_or_init(Instant::now).elapsed().as_nanos() as u64
    }
}

fn status(elapsed: u64) -> std::string::String {
    let mut sensor = Sensor::new(0, 80_000, 75_000, 1_000_000_000).unwrap();
    let mut events = EventQueue::<4>::default();
    let simulated_temperature = if elapsed < TACH_LOSS_NS { 70_000 } else { 82_000 };
    sensor.sample(elapsed, Some(simulated_temperature), &mut events).unwrap();
    let state = match sensor.state() {
        State::Normal => "normal",
        State::High => "high",
        State::Unavailable => "unavailable",
    };
    let mut power = Power::new(100_000_000, 0, 1);
    power.poll(0, Feedback::Level(false));
    power.poll(elapsed.min(20_000_000).into(), Feedback::Level(elapsed >= 20_000_000));
    let power_state = match power.state() {
        PowerState::Waiting(_) => "starting",
        PowerState::On => "on",
        PowerState::Backoff(_) => "backoff",
        PowerState::Failed => "failed",
    };
    let fan = fan_model::sample(elapsed, TACH_LOSS_NS);
    let rpm = fan.rpm.map_or_else(|| "null".into(), |value| format!("{value}"));
    format!(
        concat!(
            "{{\"simulated\":true,\"elapsed_ns\":{},",
            "\"sensor\":{{\"id\":0,\"temperature_millicelsius\":{},\"state\":\"{}\"}},",
            "\"power\":{{\"enabled\":{},\"state\":\"{}\"}},",
            "\"fan\":{{\"rpm\":{},\"duty_percent\":{},\"pwm_period_ns\":{},",
            "\"pwm_high\":{},\"tach_high\":{},\"protected\":{}}}}}"
        ),
        elapsed, sensor.value().unwrap(), state, power.enabled(), power_state,
        rpm, fan.pwm.duty_percent, Pwm::PERIOD_NS, fan.pwm.high_at(elapsed),
        fan.tach_high, fan.protected
    )
}

fn serve(mut stream: TcpStream, start: u64) -> io::Result<()> {
    let mut buffer = [0; 2048];
    let mut used = 0;
    while used < buffer.len() && !buffer[..used].windows(4).any(|bytes| bytes == b"\r\n\r\n") {
        let received = stream.read(&mut buffer[used..])?;
        if received == 0 {
            return Ok(());
        }
        used += received;
    }
    let complete = buffer[..used].windows(4).any(|bytes| bytes == b"\r\n\r\n");
    let request_line = core::str::from_utf8(&buffer[..used])
        .ok()
        .and_then(|request| request.split("\r\n").next());
    let mut words = request_line.unwrap_or_default().split_ascii_whitespace();
    let (code, body, allow) = match (complete, words.next(), words.next(), words.next(), words.next()) {
        (true, Some(method), Some(path), Some("HTTP/1.0" | "HTTP/1.1"), None) => {
            match (method, path) {
                ("GET", "/v1/status") => ("200 OK", status(clock().saturating_sub(start)), ""),
                ("GET", _) => ("404 Not Found", "{\"error\":\"not_found\"}".into(), ""),
                _ => ("405 Method Not Allowed", "{\"error\":\"read_only\"}".into(), "Allow: GET\r\n"),
            }
        }
        (false, _, _, _, _) => ("431 Request Header Fields Too Large", "{\"error\":\"header_too_large\"}".into(), ""),
        _ => ("400 Bad Request", "{\"error\":\"bad_request\"}".into(), ""),
    };
    let response = format!(
        "HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n{allow}Connection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(response.as_bytes())
}

#[cfg_attr(target_os = "none", unsafe(no_mangle))]
fn main() {
    let listener = TcpListener::bind(("0.0.0.0", PORT)).expect("bind management port");
    let start = clock();
    println!("BMC_MANAGEMENT_READY");
    loop {
        let (stream, _) = listener.accept().expect("accept management connection");
        let _ = serve(stream, start);
    }
}

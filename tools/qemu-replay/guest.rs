#![cfg_attr(target_os = "none", no_std)]
#![cfg_attr(target_os = "none", no_main)]

use axbmc_core::{EventQueue, Sensor, State};
use core::{hint::black_box, time::Duration};

#[cfg(target_os = "none")]
use axstd::println;
#[cfg(not(target_os = "none"))]
use std::println;

mod control;
#[cfg(target_os = "none")]
mod platform;

const OPERATIONS: u64 = 100_000;
const TRIALS: usize = 7;
const PERIOD_NS: u64 = 13_000_000;
const PERIODIC_SAMPLES: usize = 256;
const PERIODIC_WARMUP: usize = 16;

#[cfg(target_os = "none")]
fn now_ns() -> u128 {
    arceos_api::time::ax_monotonic_time().as_nanos()
}

#[cfg(not(target_os = "none"))]
fn now_ns() -> u128 {
    use std::{sync::OnceLock, time::Instant};
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_nanos()
}

fn sensor() -> Sensor {
    Sensor::new(0, 80_000, 75_000, 1_000_000_000).unwrap()
}

fn replay() {
    let mut sensor = sensor();
    let mut queue = EventQueue::<64>::default();
    println!("BMC_TRACE_BEGIN");
    println!("sequence,time_ns,sensor_id,from,to");
    for line in include_str!("sensor.csv").lines() {
        if line.starts_with('#') || line.trim().is_empty() {
            continue;
        }
        let mut fields = line.split(',');
        let time_ns = fields.next().unwrap().parse().unwrap();
        let action = fields.next().unwrap();
        let value = fields.next().unwrap();
        match (action, value) {
            ("sample", "error") => sensor.sample(time_ns, None, &mut queue),
            ("sample", value) => {
                sensor.sample(time_ns, Some(value.parse().unwrap()), &mut queue)
            }
            ("tick", "-") => sensor.tick(time_ns, &mut queue),
            _ => panic!("invalid trace row"),
        }
        .unwrap();
        while let Some(event) = queue.pop() {
            println!(
                "{},{},{},{:?},{:?}",
                event.sequence, event.time_ns, event.sensor_id, event.from, event.to
            );
        }
    }
    assert_eq!(queue.dropped, 0);
    println!("BMC_TRACE_END");
}

fn benchmark() {
    for (name, values, expected_events) in [
        ("stable", [Some(70_000), Some(70_000)], 1),
        ("threshold", [Some(80_000), Some(75_000)], OPERATIONS),
        ("failure", [None, Some(70_000)], OPERATIONS - 1),
    ] {
        for trial in 0..=TRIALS {
            let mut sensor = sensor();
            let mut queue = EventQueue::<64>::default();
            let mut events = 0;
            let start = now_ns();
            for i in 0..OPERATIONS {
                sensor
                    .sample(i * 1_000_000, black_box(values[(i % 2) as usize]), &mut queue)
                    .unwrap();
                while let Some(event) = queue.pop() {
                    black_box(event);
                    events += 1;
                }
            }
            let elapsed = now_ns().checked_sub(start).unwrap();
            assert_eq!(events, expected_events);
            assert_eq!(queue.dropped, 0);
            if trial > 0 {
                println!("BMC_TIMING,{name},{trial},{OPERATIONS},{events},{elapsed}");
            }
        }
    }
}

fn sleep_until(deadline: u128) {
    loop {
        let remaining = deadline.saturating_sub(now_ns());
        if remaining == 0 {
            return;
        }
        let duration = Duration::from_nanos(remaining.try_into().unwrap());
        #[cfg(target_os = "none")]
        axstd::thread::sleep(duration);
        #[cfg(not(target_os = "none"))]
        std::thread::sleep(duration);
    }
}

fn periodic_benchmark() {
    let mut sensor = sensor();
    let mut queue = EventQueue::<64>::default();
    let mut samples = [(0, 0); PERIODIC_SAMPLES];
    let start = now_ns();
    for index in 0..PERIODIC_WARMUP + PERIODIC_SAMPLES {
        let time_ns = (index as u64 + 1) * PERIOD_NS;
        let deadline = start + u128::from(time_ns);
        sleep_until(deadline);
        let awake = now_ns();
        let (value, state) = if index % 2 == 0 {
            (80_000, State::High)
        } else {
            (75_000, State::Normal)
        };
        sensor.sample(time_ns, Some(value), &mut queue).unwrap();
        let event = black_box(queue.pop().unwrap());
        let consumed = now_ns();
        assert_eq!((event.sequence, event.time_ns, event.to), (index as u64 + 1, time_ns, state));
        assert!(queue.pop().is_none());
        if let Some(sample_index) = index.checked_sub(PERIODIC_WARMUP) {
            samples[sample_index] = (awake - deadline, consumed - deadline);
        }
    }
    assert_eq!(queue.dropped, 0);
    for (index, (wake, event)) in samples.into_iter().enumerate() {
        println!("BMC_PERIODIC,{index},{PERIOD_NS},{wake},{event}");
    }
}

#[cfg_attr(target_os = "none", unsafe(no_mangle))]
fn main() {
    replay();
    benchmark();
    periodic_benchmark();
    control::run();
    println!("BMC_DONE");
}

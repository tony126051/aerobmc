use axbmc_core::{EventQueue, Sensor};
use std::{env, fs, process, time::Instant};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let path = env::args().nth(1).ok_or("usage: bmc-replay TRACE.csv")?;
    let data = fs::read_to_string(path)?;
    // Parse before timing; trace timestamps are logical, not wall-clock measurements.
    let mut trace = Vec::new();
    for line in data
        .lines()
        .filter(|l| !l.trim().is_empty() && !l.starts_with('#'))
    {
        let fields: Vec<_> = line.split(',').collect();
        if fields.len() != 3 {
            return Err("expected time_ns,sample|tick,value|error|-".into());
        }
        let time: u64 = fields[0].parse()?;
        let action = match (fields[1], fields[2]) {
            ("sample", "error") => Some(None),
            ("sample", v) => Some(Some(v.parse::<i64>()?)),
            ("tick", "-") => None,
            _ => return Err("invalid action".into()),
        };
        trace.push((time, action));
    }
    if trace.is_empty() {
        return Err("empty trace".into());
    }
    let mut sensor = Sensor::new(0, 80_000, 75_000, 1_000_000_000).unwrap();
    let mut queue = EventQueue::<64>::default();
    let mut events = Vec::with_capacity(trace.len());
    let start = Instant::now();
    for (time, action) in &trace {
        match action {
            Some(value) => sensor.sample(*time, *value, &mut queue),
            None => sensor.tick(*time, &mut queue),
        }
        .map_err(|e| format!("trace error: {e:?}"))?;
        while let Some(event) = queue.pop() {
            events.push(event);
        }
    }
    let elapsed = start.elapsed();
    println!("sequence,time_ns,sensor_id,from,to");
    for e in events {
        println!(
            "{},{},{},{:?},{:?}",
            e.sequence, e.time_ns, e.sensor_id, e.from, e.to
        );
    }
    eprintln!(
        "environment={},operations={},elapsed_ns={},drops={}; smoke timing only",
        env::consts::OS,
        trace.len(),
        elapsed.as_nanos(),
        queue.dropped
    );
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("{e}");
        process::exit(1);
    }
}

pub const CAPACITY: usize = 32;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind {
    PowerStarted,
    PowerStopped,
    PowerCycleScheduled,
    PowerCycleStarted,
    PowerTimedOut,
    PowerRetryScheduled,
    PowerRetryStarted,
    PowerLocked,
    WatchdogTripped,
    PowerGoodLost,
    PowerOn,
    PowerGoodIgnored,
    FanProtection,
    FanRecovered,
    FanResetRejected,
    SensorHigh(u8),
    SensorCleared(u8),
    SensorStale(u8),
    SensorRecovered(u8),
    SensorCritical(u8),
    SensorCriticalCleared(u8),
    ThermalTripped(u8),
    ThermalTelemetryLost,
    ThermalStartBlocked,
}

impl EventKind {
    pub fn name(self) -> &'static str {
        match self {
            Self::PowerStarted => "power_started",
            Self::PowerStopped => "power_stopped",
            Self::PowerCycleScheduled => "power_cycle_scheduled",
            Self::PowerCycleStarted => "power_cycle_started",
            Self::PowerTimedOut => "power_timed_out",
            Self::PowerRetryScheduled => "power_retry_scheduled",
            Self::PowerRetryStarted => "power_retry_started",
            Self::PowerLocked => "power_locked",
            Self::WatchdogTripped => "watchdog_tripped",
            Self::PowerGoodLost => "power_good_lost",
            Self::PowerOn => "power_on",
            Self::PowerGoodIgnored => "power_good_ignored",
            Self::FanProtection => "fan_protection",
            Self::FanRecovered => "fan_recovered",
            Self::FanResetRejected => "fan_reset_rejected",
            Self::SensorHigh(_) => "sensor_high",
            Self::SensorCleared(_) => "sensor_cleared",
            Self::SensorStale(_) => "sensor_stale",
            Self::SensorRecovered(_) => "sensor_recovered",
            Self::SensorCritical(_) => "sensor_critical",
            Self::SensorCriticalCleared(_) => "sensor_critical_cleared",
            Self::ThermalTripped(_) => "thermal_tripped",
            Self::ThermalTelemetryLost => "thermal_telemetry_lost",
            Self::ThermalStartBlocked => "thermal_start_blocked",
        }
    }

    pub fn sensor_id(self) -> Option<u8> {
        match self {
            Self::SensorHigh(id) | Self::SensorCleared(id) | Self::SensorStale(id)
            | Self::SensorRecovered(id) | Self::SensorCritical(id)
            | Self::SensorCriticalCleared(id) | Self::ThermalTripped(id) => Some(id),
            _ => None,
        }
    }
}

#[derive(Clone, Copy)]
pub struct Event {
    pub sequence: u64,
    pub at_ms: u64,
    pub revision: u64,
    pub kind: EventKind,
}

#[derive(Clone, Copy)]
pub struct EventLog {
    entries: [Option<Event>; CAPACITY],
    next: usize,
    total: u64,
}

impl EventLog {
    pub fn new() -> Self {
        Self { entries: [None; CAPACITY], next: 0, total: 0 }
    }

    pub fn append(&mut self, at_ms: u64, revision: u64, kind: EventKind) {
        self.total += 1;
        self.entries[self.next] = Some(Event { sequence: self.total, at_ms, revision, kind });
        self.next = (self.next + 1) % CAPACITY;
    }

    pub fn overwritten(&self) -> u64 {
        self.total.saturating_sub(CAPACITY as u64)
    }

    pub fn iter(&self) -> impl Iterator<Item = &Event> {
        self.entries[self.next..].iter().chain(&self.entries[..self.next]).flatten()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_chronological_order_across_wrap_and_reports_overwrites() {
        let mut log = EventLog::new();
        for value in 1..=40 {
            log.append(value, value, EventKind::PowerStarted);
        }
        assert_eq!(log.overwritten(), 8);
        assert!(log.iter().map(|event| event.sequence).eq(9..=40));
        assert!(log.iter().all(|event| event.at_ms == event.sequence));
    }
}

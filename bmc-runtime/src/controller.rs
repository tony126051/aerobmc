use crate::events::{EventKind, EventLog};
use crate::sensor::Sensor;

const POWER_TIMEOUT_MS: u64 = 100;
const RETRY_DELAY_MS: u64 = 100;
const CYCLE_OFF_MS: u64 = 100;
pub const POWER_ATTEMPT_LIMIT: u8 = 3;
const TACH_TIMEOUT_MS: u64 = 100;
const FAN_RECOVERY_MS: u64 = 100;
const FAN_RECOVERY_MIN_RPM: u32 = 2_000;
pub const WATCHDOG_TIMEOUT_MS: u64 = 500;
pub const POWER_GOOD_LOSS_MS: u64 = 50;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Power {
    Off,
    Starting { deadline_ms: u64 },
    RetryWait { until_ms: u64 },
    CycleWait { until_ms: Option<u64> },
    On,
    Failed,
    WatchdogTripped,
    PowerGoodLost,
    ThermalTripped,
    ThermalTelemetryLost,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Input {
    Start,
    Stop,
    Cycle,
    FanReset,
    PowerGood,
    PowerGoodAt(u64),
    HeartbeatAt(u64),
    Temperature { sensor: u8, value: i32 },
    Tach(u32),
}

#[derive(Clone, Copy)]
pub struct Controller {
    pub power: Power,
    pub sensors: [Sensor; 2],
    pub rpm: Option<u32>,
    pub fan_protected: bool,
    pub revision: u64,
    pub control_progress: u64,
    pub power_good_edges: u64,
    pub power_attempt: u8,
    pub heartbeat_edges: u64,
    pub watchdog_last_ms: Option<u64>,
    pub watchdog_trips: u64,
    attempt_started_ms: u64,
    power_good_low_since_ms: Option<u64>,
    pub events: EventLog,
    last_tach_ms: Option<u64>,
    fan_recovery_since_ms: Option<u64>,
}

impl Controller {
    pub fn new() -> Self {
        Self {
            power: Power::Off,
            sensors: [
                Sensor::new("inlet", 80_000, 75_000, 95_000, 85_000),
                Sensor::new("board", 65_000, 60_000, 85_000, 75_000),
            ],
            rpm: None,
            fan_protected: false,
            revision: 0,
            control_progress: 0,
            power_good_edges: 0,
            power_attempt: 0,
            heartbeat_edges: 0,
            watchdog_last_ms: None,
            watchdog_trips: 0,
            attempt_started_ms: 0,
            power_good_low_since_ms: None,
            events: EventLog::new(),
            last_tach_ms: None,
            fan_recovery_since_ms: None,
        }
    }

    pub fn fan_duty(&self) -> u8 {
        if self.fan_protected || self.sensors.iter().any(|sensor| sensor.critical) {
            100
        } else if self.sensors.iter().any(|sensor| sensor.stale || sensor.high) {
            75
        } else {
            50
        }
    }

    pub fn tick(&mut self, now_ms: u64) {
        for id in 0..self.sensors.len() {
            if self.sensors[id].expire(now_ms) {
                self.record(now_ms, EventKind::SensorStale(id as u8));
            }
        }
        if self.thermal_telemetry_lost() {
            self.trip_thermal(now_ms, Power::ThermalTelemetryLost, EventKind::ThermalTelemetryLost);
        }
        match self.power {
            Power::Starting { deadline_ms } if now_ms >= deadline_ms => {
                self.record(now_ms, EventKind::PowerTimedOut);
                if self.power_attempt < POWER_ATTEMPT_LIMIT {
                    self.power = Power::RetryWait { until_ms: now_ms.saturating_add(RETRY_DELAY_MS) };
                    self.record(now_ms, EventKind::PowerRetryScheduled);
                } else {
                    self.power = Power::Failed;
                    self.record(now_ms, EventKind::PowerLocked);
                }
            }
            Power::On if self.watchdog_last_ms.is_some_and(|last|
                now_ms.saturating_sub(last) >= WATCHDOG_TIMEOUT_MS) =>
            {
                self.power = Power::WatchdogTripped;
                self.watchdog_last_ms = None;
                self.watchdog_trips += 1;
                self.record(now_ms, EventKind::WatchdogTripped);
            }
            Power::RetryWait { until_ms } if now_ms >= until_ms => {
                self.start_attempt(now_ms, EventKind::PowerRetryStarted);
            }
            Power::CycleWait { until_ms: Some(until_ms) } if now_ms >= until_ms => {
                self.start_attempt(now_ms, EventKind::PowerCycleStarted);
            }
            _ => {}
        }
        if self.last_tach_ms.is_some_and(|last| now_ms.saturating_sub(last) >= TACH_TIMEOUT_MS)
            && (!self.fan_protected || self.rpm.is_some())
        {
            self.rpm = None;
            self.fan_recovery_since_ms = None;
            if !self.fan_protected {
                self.fan_protected = true;
                self.record(now_ms, EventKind::FanProtection);
            } else {
                self.revision += 1;
            }
        }
    }

    fn record(&mut self, now_ms: u64, kind: EventKind) {
        self.revision += 1;
        self.events.append(now_ms, self.revision, kind);
    }

    fn thermal_telemetry_lost(&self) -> bool {
        self.sensors.iter().any(|sensor| sensor.stale)
            && self.sensors.iter().all(|sensor| sensor.value.is_none())
    }

    fn trip_thermal(&mut self, now_ms: u64, fault: Power, event: EventKind) {
        if matches!(self.power,
            Power::Starting { .. } | Power::RetryWait { .. } | Power::CycleWait { .. } | Power::On)
        {
            self.power = fault;
            self.watchdog_last_ms = None;
            self.power_good_low_since_ms = None;
            self.record(now_ms, event);
        }
    }

    pub fn outputs_published(&mut self, now_ms: u64) {
        if let Power::CycleWait { until_ms: None } = self.power {
            // Round up so truncating the clock to milliseconds cannot shorten the dwell.
            self.power = Power::CycleWait { until_ms: Some(now_ms.saturating_add(CYCLE_OFF_MS + 1)) };
        }
    }

    fn start_attempt(&mut self, now_ms: u64, kind: EventKind) {
        self.power_attempt += 1;
        self.attempt_started_ms = now_ms;
        self.power_good_low_since_ms = None;
        self.power = Power::Starting { deadline_ms: now_ms.saturating_add(POWER_TIMEOUT_MS) };
        self.record(now_ms, kind);
    }

    pub fn observe_power_good(&mut self, now_ms: u64, high: bool) {
        if self.power != Power::On {
            return;
        }
        if high {
            self.power_good_low_since_ms = None;
            return;
        }
        let since = *self.power_good_low_since_ms.get_or_insert(now_ms);
        if now_ms.saturating_sub(since) >= POWER_GOOD_LOSS_MS {
            self.tick(now_ms);
            if self.power != Power::On {
                return;
            }
            self.power = Power::PowerGoodLost;
            self.watchdog_last_ms = None;
            self.record(now_ms, EventKind::PowerGoodLost);
        }
    }

    pub fn apply(&mut self, now_ms: u64, input: Input) -> Option<EventKind> {
        if matches!(input, Input::Stop) {
            if matches!(self.power,
                Power::Starting { .. } | Power::RetryWait { .. } | Power::CycleWait { .. } | Power::On)
            {
                self.power = Power::Off;
                self.power_attempt = 0;
                self.watchdog_last_ms = None;
                self.power_good_low_since_ms = None;
                self.record(now_ms, EventKind::PowerStopped);
                return Some(EventKind::PowerStopped);
            }
            return None;
        }
        let previous_attempt = self.power_attempt;
        let was_starting = matches!(self.power, Power::Starting { .. });
        self.tick(now_ms);
        match input {
            Input::FanReset if self.fan_protected => {
                let recovered = self.fan_recovery_since_ms.zip(self.last_tach_ms)
                    .is_some_and(|(since, last)| last.saturating_sub(since) >= FAN_RECOVERY_MS);
                let kind = if recovered {
                    self.fan_protected = false;
                    self.fan_recovery_since_ms = None;
                    EventKind::FanRecovered
                } else {
                    EventKind::FanResetRejected
                };
                self.record(now_ms, kind);
                return Some(kind);
            }
            Input::Cycle if self.power == Power::On => {
                self.power = Power::CycleWait { until_ms: None };
                self.power_attempt = 0;
                self.watchdog_last_ms = None;
                self.power_good_low_since_ms = None;
                self.record(now_ms, EventKind::PowerCycleScheduled);
                return Some(EventKind::PowerCycleScheduled);
            }
            Input::Start if matches!(self.power,
                Power::Off | Power::Failed | Power::WatchdogTripped | Power::PowerGoodLost
                | Power::ThermalTripped | Power::ThermalTelemetryLost) =>
            {
                if self.thermal_telemetry_lost() || self.sensors.iter().any(|sensor| sensor.critical) {
                    self.record(now_ms, EventKind::ThermalStartBlocked);
                    return Some(EventKind::ThermalStartBlocked);
                }
                self.last_tach_ms.get_or_insert(now_ms);
                self.power_attempt = 0;
                self.start_attempt(now_ms, EventKind::PowerStarted);
                return Some(EventKind::PowerStarted);
            }
            Input::PowerGood | Input::PowerGoodAt(_) => {
                self.power_good_edges += 1;
                let captured = match input {
                    Input::PowerGoodAt(time) => time,
                    _ => now_ms,
                };
                let kind = if was_starting && previous_attempt == self.power_attempt
                    && matches!(self.power, Power::Starting { .. })
                    && captured >= self.attempt_started_ms && captured <= now_ms
                {
                    self.power = Power::On;
                    self.watchdog_last_ms = Some(now_ms);
                    EventKind::PowerOn
                } else {
                    EventKind::PowerGoodIgnored
                };
                self.record(now_ms, kind);
                return None;
            }
            Input::HeartbeatAt(captured) => {
                self.heartbeat_edges += 1;
                if self.power == Power::On && captured <= now_ms
                    && self.watchdog_last_ms.is_some_and(|last| captured > last)
                {
                    self.watchdog_last_ms = Some(captured);
                }
            }
            Input::Temperature { sensor, value } if (-40_000..=150_000).contains(&value) => {
                let Some(reading) = self.sensors.get_mut(usize::from(sensor)) else {
                    return None;
                };
                let was_stale = reading.stale;
                let was_high = reading.high;
                let was_critical = reading.critical;
                reading.sample(now_ms, value);
                let high = reading.high;
                let critical = reading.critical;
                if was_stale {
                    self.record(now_ms, EventKind::SensorRecovered(sensor));
                }
                if high != was_high {
                    let kind = if high { EventKind::SensorHigh(sensor) } else { EventKind::SensorCleared(sensor) };
                    self.record(now_ms, kind);
                } else if !was_stale && critical == was_critical {
                    self.revision += 1;
                }
                if critical != was_critical {
                    self.record(now_ms, if critical {
                        EventKind::SensorCritical(sensor)
                    } else {
                        EventKind::SensorCriticalCleared(sensor)
                    });
                }
                if critical {
                    self.trip_thermal(now_ms, Power::ThermalTripped, EventKind::ThermalTripped(sensor));
                }
                return None;
            }
            Input::Tach(rpm) if rpm > 0 => {
                if self.fan_protected && rpm >= FAN_RECOVERY_MIN_RPM {
                    self.fan_recovery_since_ms.get_or_insert(now_ms);
                } else {
                    self.fan_recovery_since_ms = None;
                }
                self.last_tach_ms = Some(now_ms);
                self.rpm = Some(rpm);
            }
            _ => return None,
        }
        self.revision += 1;
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_alarm_keeps_boost_and_recovery_preserves_hysteresis() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Temperature { sensor: 0, value: 82_000 });
        bmc.tick(999);
        assert_eq!(bmc.sensors[0].value, Some(82_000));
        bmc.tick(1000);
        assert_eq!(bmc.sensors[0].state(), "stale");
        assert_eq!(bmc.fan_duty(), 75);
        bmc.apply(1001, Input::Temperature { sensor: 0, value: 150_001 });
        assert_eq!(bmc.sensors[0].value, None);
        bmc.apply(1002, Input::Temperature { sensor: 0, value: 77_000 });
        assert_eq!(bmc.sensors[0].state(), "high");
        bmc.apply(1003, Input::Temperature { sensor: 0, value: 75_000 });
        assert_eq!(bmc.fan_duty(), 50);
        assert!(bmc.events.iter().map(|event| event.kind).eq([
            EventKind::SensorHigh(0), EventKind::SensorStale(0),
            EventKind::SensorRecovered(0), EventKind::SensorCleared(0),
        ]));
    }

    #[test]
    fn retries_are_bounded_and_feedback_cannot_cross_attempts() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Start);
        bmc.apply(100, Input::PowerGood);
        assert_eq!(bmc.power, Power::RetryWait { until_ms: 200 });
        bmc.apply(199, Input::Start);
        assert_eq!(bmc.power_attempt, 1);
        bmc.apply(200, Input::PowerGoodAt(199));
        assert_eq!(bmc.power, Power::Starting { deadline_ms: 300 });
        bmc.apply(201, Input::PowerGoodAt(199));
        assert_eq!(bmc.power_attempt, 2);
        assert_eq!(bmc.power, Power::Starting { deadline_ms: 300 });
        bmc.tick(300);
        bmc.tick(400);
        assert_eq!(bmc.power_attempt, 3);
        bmc.tick(500);
        bmc.apply(501, Input::PowerGood);
        bmc.tick(1_000);
        assert_eq!(bmc.power, Power::Failed);
        assert_eq!(bmc.power_attempt, 3);
        bmc.apply(1_001, Input::Start);
        bmc.apply(1_020, Input::PowerGood);
        assert_eq!(bmc.power, Power::On);
        assert_eq!(bmc.power_attempt, 1);
    }

    #[test]
    fn watchdog_refreshes_on_fresh_edges_and_latches_at_deadline() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Start);
        bmc.apply(20, Input::PowerGood);
        bmc.apply(419, Input::HeartbeatAt(400));
        bmc.apply(600, Input::HeartbeatAt(399));
        assert_eq!(bmc.watchdog_last_ms, Some(400));
        bmc.tick(899);
        assert_eq!(bmc.power, Power::On);
        bmc.apply(900, Input::HeartbeatAt(899));
        assert_eq!(bmc.power, Power::WatchdogTripped);
        bmc.apply(901, Input::HeartbeatAt(901));
        bmc.tick(2_000);
        assert_eq!(bmc.power, Power::WatchdogTripped);
        assert_eq!(bmc.watchdog_trips, 1);
        bmc.apply(2_001, Input::Start);
        bmc.apply(2_010, Input::PowerGood);
        assert_eq!(bmc.watchdog_last_ms, Some(2_010));
        assert_eq!(bmc.power, Power::On);
    }

    #[test]
    fn power_good_loss_requires_sustained_low_and_explicit_restart() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Start);
        bmc.observe_power_good(10, false);
        bmc.apply(20, Input::PowerGood);
        bmc.observe_power_good(30, false);
        bmc.observe_power_good(79, false);
        assert_eq!(bmc.power, Power::On);
        bmc.observe_power_good(79, true);
        bmc.observe_power_good(80, false);
        bmc.observe_power_good(129, false);
        assert_eq!(bmc.power, Power::On);
        bmc.observe_power_good(130, false);
        assert_eq!(bmc.power, Power::PowerGoodLost);
        assert_eq!(bmc.watchdog_last_ms, None);
        bmc.apply(131, Input::PowerGood);
        bmc.apply(132, Input::HeartbeatAt(132));
        bmc.tick(1_000);
        assert_eq!(bmc.power, Power::PowerGoodLost);
        assert_eq!(bmc.watchdog_trips, 0);
        assert_eq!(bmc.events.iter().filter(|event| event.kind == EventKind::PowerGoodLost).count(), 1);
        bmc.apply(1_001, Input::Start);
        bmc.apply(1_010, Input::PowerGood);
        assert_eq!(bmc.power, Power::On);
        bmc.observe_power_good(1_011, false);
        assert_eq!(bmc.power, Power::On);
    }

    #[test]
    fn stop_cancels_startup_and_retry_even_at_the_deadline() {
        for cancel_at in [25, 200] {
            let mut bmc = Controller::new();
            bmc.apply(0, Input::Start);
            if cancel_at == 200 {
                bmc.tick(100);
                assert_eq!(bmc.power, Power::RetryWait { until_ms: 200 });
            }
            bmc.apply(cancel_at, Input::Stop);
            let revision = bmc.revision;
            bmc.apply(cancel_at + 1, Input::Stop);
            assert_eq!(bmc.revision, revision);
            bmc.apply(cancel_at + 2, Input::PowerGood);
            bmc.tick(1_000);
            assert_eq!(bmc.power, Power::Off);
            assert_eq!(bmc.power_attempt, 0);
            assert!(!bmc.events.iter().any(|event| event.kind == EventKind::PowerRetryStarted));
            bmc.apply(1_001, Input::Start);
            bmc.apply(1_002, Input::PowerGoodAt(cancel_at + 2));
            assert!(matches!(bmc.power, Power::Starting { .. }));
            bmc.apply(1_003, Input::PowerGoodAt(1_003));
            assert_eq!(bmc.power, Power::On);
        }
    }

    #[test]
    fn stop_disarms_watchdog_and_preserves_existing_faults() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Start);
        bmc.apply(10, Input::PowerGood);
        bmc.apply(510, Input::Stop);
        bmc.tick(1_000);
        assert_eq!(bmc.power, Power::Off);
        assert_eq!(bmc.watchdog_last_ms, None);
        assert_eq!(bmc.watchdog_trips, 0);
        bmc.apply(1_001, Input::Start);
        bmc.apply(1_010, Input::PowerGood);
        bmc.tick(1_510);
        bmc.apply(1_511, Input::Stop);
        assert_eq!(bmc.power, Power::WatchdogTripped);
        assert_eq!(bmc.watchdog_trips, 1);
    }

    #[test]
    fn cycle_waits_for_output_publication_and_fresh_feedback() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Start);
        bmc.apply(10, Input::PowerGood);
        bmc.apply(20, Input::Cycle);
        bmc.tick(150);
        assert_eq!(bmc.power, Power::CycleWait { until_ms: None });
        bmc.outputs_published(150);
        bmc.apply(160, Input::Start);
        bmc.apply(170, Input::Cycle);
        bmc.apply(250, Input::PowerGoodAt(249));
        assert_eq!(bmc.power, Power::CycleWait { until_ms: Some(251) });
        assert_eq!(bmc.watchdog_last_ms, None);
        bmc.tick(251);
        assert_eq!(bmc.power_attempt, 1);
        bmc.apply(252, Input::PowerGoodAt(250));
        assert_eq!(bmc.power, Power::Starting { deadline_ms: 351 });
        bmc.apply(253, Input::PowerGoodAt(253));
        assert_eq!(bmc.power, Power::On);
        assert_eq!(bmc.watchdog_last_ms, Some(253));

        bmc.apply(260, Input::Cycle);
        bmc.outputs_published(260);
        bmc.apply(361, Input::Stop);
        bmc.tick(700);
        assert_eq!(bmc.power, Power::Off);
        assert_eq!(bmc.events.iter().filter(|event| event.kind == EventKind::PowerCycleStarted).count(), 1);
    }

    #[test]
    fn fan_reset_requires_fresh_sustained_tach_and_restores_thermal_control() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Tach(3_000));
        bmc.tick(100);
        bmc.apply(101, Input::FanReset);
        assert_eq!(bmc.fan_duty(), 100);
        for (at, rpm) in [(110, 3_000), (160, 3_000), (210, 1_999), (260, 3_000), (310, 3_000)] {
            bmc.apply(at, Input::Tach(rpm));
        }
        bmc.apply(311, Input::FanReset);
        assert_eq!(bmc.fan_duty(), 100);
        bmc.apply(360, Input::Tach(3_000));
        bmc.apply(460, Input::FanReset);
        assert_eq!(bmc.fan_duty(), 100);
        assert_eq!(bmc.rpm, None);

        bmc.apply(461, Input::Temperature { sensor: 1, value: 68_000 });
        for at in [470, 520, 570] {
            bmc.apply(at, Input::Tach(3_000));
        }
        assert_eq!(bmc.fan_duty(), 100);
        bmc.apply(571, Input::FanReset);
        assert_eq!(bmc.fan_duty(), 75);
        let revision = bmc.revision;
        bmc.apply(572, Input::FanReset);
        assert_eq!(bmc.revision, revision);
        bmc.tick(670);
        assert_eq!(bmc.fan_duty(), 100);
        assert_eq!(bmc.events.iter().filter(|event| event.kind == EventKind::FanResetRejected).count(), 3);
    }

    #[test]
    fn thermal_trip_blocks_power_until_all_critical_sensors_cool() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Start);
        bmc.apply(10, Input::PowerGood);
        bmc.apply(20, Input::Temperature { sensor: 1, value: 85_000 });
        assert_eq!(bmc.power, Power::ThermalTripped);
        assert_eq!(bmc.watchdog_last_ms, None);
        assert!(!bmc.fan_protected);
        assert_eq!(bmc.fan_duty(), 100);
        bmc.apply(21, Input::Temperature { sensor: 0, value: 95_000 });
        bmc.apply(22, Input::Temperature { sensor: 1, value: 75_001 });
        bmc.tick(1_022);
        assert!(bmc.sensors.iter().all(|sensor| sensor.stale && sensor.critical));
        bmc.apply(1_023, Input::Temperature { sensor: 1, value: 75_000 });
        bmc.apply(1_024, Input::Start);
        assert_eq!(bmc.power, Power::ThermalTripped);
        bmc.apply(1_025, Input::Temperature { sensor: 0, value: 85_000 });
        assert_eq!(bmc.power, Power::ThermalTripped);
        bmc.apply(1_026, Input::Start);
        bmc.apply(1_027, Input::PowerGood);
        assert_eq!(bmc.power, Power::On);
        bmc.apply(1_028, Input::Cycle);
        bmc.outputs_published(1_028);
        bmc.apply(1_029, Input::Temperature { sensor: 0, value: 95_000 });
        bmc.tick(1_200);
        assert_eq!(bmc.power, Power::ThermalTripped);
    }

    #[test]
    fn total_telemetry_loss_cancels_power_and_requires_a_new_start() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Temperature { sensor: 0, value: 40_000 });
        bmc.apply(500, Input::Temperature { sensor: 1, value: 55_000 });
        bmc.apply(1_000, Input::Start);
        bmc.apply(1_010, Input::PowerGood);
        for at in [1_080, 1_160, 1_240, 1_320, 1_400] {
            bmc.apply(at, Input::Tach(3_000));
        }
        bmc.apply(1_400, Input::HeartbeatAt(1_400));
        assert_eq!(bmc.power, Power::On);
        assert_eq!(bmc.fan_duty(), 75);
        bmc.apply(1_500, Input::Start);
        assert_eq!(bmc.power, Power::ThermalTelemetryLost);
        assert_eq!(bmc.watchdog_last_ms, None);
        bmc.apply(1_501, Input::PowerGood);
        bmc.apply(1_502, Input::Temperature { sensor: 1, value: 150_001 });
        assert_eq!(bmc.power, Power::ThermalTelemetryLost);
        bmc.apply(1_503, Input::Temperature { sensor: 1, value: 55_000 });
        assert_eq!(bmc.power, Power::ThermalTelemetryLost);
        bmc.apply(1_504, Input::Start);
        bmc.apply(1_505, Input::PowerGood);
        assert_eq!(bmc.power, Power::On);
        assert_eq!(bmc.events.iter().filter(|event| event.kind == EventKind::ThermalTelemetryLost).count(), 1);
    }

    #[test]
    fn confirmed_retry_cancels_further_attempts() {
        let mut bmc = Controller::new();
        bmc.apply(0, Input::Start);
        bmc.tick(100);
        bmc.tick(200);
        bmc.apply(220, Input::PowerGoodAt(215));
        bmc.tick(500);
        assert_eq!(bmc.power, Power::On);
        assert_eq!(bmc.power_attempt, 2);
    }
}

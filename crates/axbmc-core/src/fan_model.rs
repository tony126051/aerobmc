//! Ideal PWM and tach waveforms for the software fixture.

use crate::fan::Fan;

const NS_PER_MINUTE: u128 = 60_000_000_000;
const MAX_RPM: u32 = 6_000;
const PULSES_PER_REVOLUTION: u8 = 2;
const LOSS_NS: u64 = 100_000_000;
const NORMAL_DUTY: u8 = 50;
const PROTECTION_DUTY: u8 = 100;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Pwm {
    pub duty_percent: u8,
}

impl Pwm {
    pub const PERIOD_NS: u64 = 40_000;

    pub fn high_at(self, time_ns: u64) -> bool {
        time_ns % Self::PERIOD_NS < Self::PERIOD_NS * u64::from(self.duty_percent) / 100
    }
}

#[derive(Default)]
pub struct Tachometer {
    latest_ns: Option<u64>,
    interval_ns: Option<u64>,
}

impl Tachometer {
    /// Ignore duplicate and out-of-order edge timestamps.
    pub fn rising_edge(&mut self, time_ns: u64) {
        if let Some(latest) = self.latest_ns {
            if time_ns <= latest {
                return;
            }
            self.interval_ns = Some(time_ns - latest);
        }
        self.latest_ns = Some(time_ns);
    }

    pub fn rpm_at(&self, now_ns: u64, pulses_per_revolution: u8, stale_ns: u64) -> Option<u32> {
        let age = now_ns.checked_sub(self.latest_ns?)?;
        if age >= stale_ns || pulses_per_revolution == 0 {
            return None;
        }
        let divisor = u128::from(self.interval_ns?) * u128::from(pulses_per_revolution);
        (NS_PER_MINUTE / divisor).try_into().ok()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FanSnapshot {
    pub pwm: Pwm,
    pub rpm: Option<u32>,
    pub protected: bool,
    pub tach_high: bool,
}

/// Linear duty-to-speed fixture. Missing tach is an injected feedback failure.
/// Sample time is independent of management requests and can jump forward.
pub fn sample(now_ns: u64, tach_loss_ns: u64) -> FanSnapshot {
    let ideal_rpm = u128::from(MAX_RPM) * u128::from(NORMAL_DUTY) / 100;
    let period = (NS_PER_MINUTE / (ideal_rpm * u128::from(PULSES_PER_REVOLUTION))) as u64;
    let last_edge = now_ns.min(tach_loss_ns.saturating_sub(1)) / period * period;
    let mut tach = Tachometer::default();
    if tach_loss_ns != 0 {
        if last_edge >= period {
            tach.rising_edge(last_edge - period);
        }
        tach.rising_edge(last_edge);
    }
    let mut fan = Fan::new(NORMAL_DUTY, PROTECTION_DUTY, LOSS_NS.into());
    let age = now_ns - last_edge;
    fan.poll(last_edge.into(), None);
    fan.poll(now_ns.into(), None);
    FanSnapshot {
        pwm: Pwm { duty_percent: fan.pwm_percent() },
        rpm: tach.rpm_at(now_ns, PULSES_PER_REVOLUTION, LOSS_NS),
        protected: age >= LOSS_NS && fan.protected_at().is_some(),
        tach_high: now_ns < tach_loss_ns && now_ns % period < period / 2,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn waveform_matches_duty_and_wraps() {
        let pwm = Pwm { duty_percent: 50 };
        assert!(pwm.high_at(19_999));
        assert!(!pwm.high_at(20_000));
        assert!(pwm.high_at(40_000));
        assert!(!Pwm { duty_percent: 0 }.high_at(0));
        assert!(Pwm { duty_percent: 100 }.high_at(39_999));
    }

    #[test]
    fn pulses_measure_rpm_and_expire_at_the_watchdog_boundary() {
        let loss = 50_000_000;
        let active = sample(23_000_000, loss);
        assert_eq!(active.rpm, Some(3000));
        assert!(active.tach_high);
        assert!(!sample(27_000_000, loss).tach_high);
        let pending = sample(139_999_999, loss);
        assert_eq!(pending.rpm, Some(3000));
        assert!(!pending.protected);
        let failed = sample(140_000_000, loss);
        assert_eq!(failed.rpm, None);
        assert_eq!(failed.pwm.duty_percent, 100);
        assert!(failed.protected);
        assert!(!failed.tach_high);
    }
}

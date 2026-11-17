//! Latched fan protection for a monotonic nanosecond clock.

pub struct Fan {
    pwm_percent: u8,
    protection_percent: u8,
    timeout_ns: u128,
    missing_since: Option<u128>,
    protected_at: Option<u128>,
}

impl Fan {
    pub fn new(normal_percent: u8, protection_percent: u8, timeout_ns: u128) -> Self {
        assert!(normal_percent <= protection_percent && protection_percent <= 100 && timeout_ns > 0);
        Self {
            pwm_percent: normal_percent,
            protection_percent,
            timeout_ns,
            missing_since: None,
            protected_at: None,
        }
    }

    pub fn pwm_percent(&self) -> u8 {
        self.pwm_percent
    }

    pub fn protected_at(&self) -> Option<u128> {
        self.protected_at
    }

    pub fn poll(&mut self, now: u128, rpm: Option<u16>) {
        if rpm.is_some_and(|speed| speed > 0) {
            self.missing_since = None;
        } else {
            let missing_since = *self.missing_since.get_or_insert(now);
            if now.saturating_sub(missing_since) >= self.timeout_ns && self.protected_at.is_none() {
                self.pwm_percent = self.protection_percent;
                self.protected_at = Some(now);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Fan;

    #[test]
    fn valid_rpm_resets_loss_window_and_protection_latches() {
        let mut fan = Fan::new(50, 100, 10);
        fan.poll(0, None);
        fan.poll(9, Some(1500));
        fan.poll(10, Some(0));
        fan.poll(19, None);
        assert_eq!(fan.pwm_percent(), 50);
        fan.poll(20, None);
        assert_eq!(fan.protected_at(), Some(20));
        fan.poll(21, Some(1500));
        assert_eq!(fan.pwm_percent(), 100);
        assert_eq!(fan.protected_at(), Some(20));
    }
}

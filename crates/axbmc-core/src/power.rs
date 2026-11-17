//! Power sequencing for one serialized owner using a monotonic nanosecond clock.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PowerState {
    Waiting(u128),
    Backoff(u128),
    On,
    Failed,
}

#[derive(Clone, Copy)]
pub struct PowerGoodEdge {
    pub attempt: u8,
    pub captured_ns: u128,
}

#[derive(Clone, Copy)]
pub enum Feedback {
    Level(bool),
    Timestamped {
        level_high: bool,
        edge: Option<PowerGoodEdge>,
    },
}

pub struct Power {
    timeout_ns: u128,
    retry_ns: u128,
    max_attempts: u8,
    attempts: u8,
    timeouts: u8,
    worst_timeout_lateness: u128,
    state: PowerState,
}

impl Power {
    pub fn new(timeout_ns: u128, retry_ns: u128, max_attempts: u8) -> Self {
        assert!(timeout_ns > 0 && max_attempts > 0);
        Self {
            timeout_ns,
            retry_ns,
            max_attempts,
            attempts: 0,
            timeouts: 0,
            worst_timeout_lateness: 0,
            state: PowerState::Backoff(0),
        }
    }

    pub fn state(&self) -> PowerState {
        self.state
    }

    pub fn enabled(&self) -> bool {
        matches!(self.state, PowerState::Waiting(_) | PowerState::On)
    }

    pub fn attempts(&self) -> u8 {
        self.attempts
    }

    pub fn timeouts(&self) -> u8 {
        self.timeouts
    }

    pub fn worst_timeout_lateness(&self) -> u128 {
        self.worst_timeout_lateness
    }

    pub fn poll(&mut self, now: u128, feedback: Feedback) {
        if let PowerState::Waiting(started) = self.state {
            match feedback {
                Feedback::Level(true) if now >= started && now - started < self.timeout_ns => {
                    self.state = PowerState::On;
                }
                Feedback::Timestamped { level_high, edge: Some(edge) } => {
                    self.on_power_good_irq(now, edge, level_high);
                }
                _ => {}
            }
            self.on_timeout(now, self.attempts);
        } else if let PowerState::Backoff(retry_at) = self.state {
            if now >= retry_at {
                self.attempts += 1;
                self.state = PowerState::Waiting(now);
            }
        }
    }

    pub fn on_timeout(&mut self, now: u128, attempt: u8) {
        let PowerState::Waiting(started) = self.state else {
            return;
        };
        if attempt != self.attempts || now < started || now - started < self.timeout_ns {
            return;
        }
        self.timeouts += 1;
        self.worst_timeout_lateness = self.worst_timeout_lateness.max(now - started - self.timeout_ns);
        self.state = if self.attempts == self.max_attempts {
            PowerState::Failed
        } else {
            PowerState::Backoff(now.saturating_add(self.retry_ns))
        };
    }

    pub fn on_power_good_irq(&mut self, now: u128, edge: PowerGoodEdge, level_high: bool) {
        let PowerState::Waiting(started) = self.state else {
            return;
        };
        if level_high && edge.attempt == self.attempts
            && edge.captured_ns >= started && edge.captured_ns <= now
            && edge.captured_ns - started < self.timeout_ns
        {
            self.state = PowerState::On;
        }
    }
}

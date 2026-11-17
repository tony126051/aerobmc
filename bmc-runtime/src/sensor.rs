pub const STALE_MS: u64 = 1_000;

#[derive(Clone, Copy)]
pub struct Sensor {
    pub name: &'static str,
    pub high_mc: i32,
    pub clear_mc: i32,
    critical_mc: i32,
    critical_clear_mc: i32,
    pub value: Option<i32>,
    pub high: bool,
    pub critical: bool,
    pub stale: bool,
    sampled_ms: Option<u64>,
}

impl Sensor {
    pub const fn new(name: &'static str, high_mc: i32, clear_mc: i32,
                     critical_mc: i32, critical_clear_mc: i32) -> Self {
        Self {
            name, high_mc, clear_mc, critical_mc, critical_clear_mc,
            value: None, high: false, critical: false, stale: false, sampled_ms: None,
        }
    }

    pub fn sample(&mut self, now_ms: u64, value: i32) {
        self.value = Some(value);
        self.sampled_ms = Some(now_ms);
        self.stale = false;
        self.high = if self.high { value > self.clear_mc } else { value >= self.high_mc };
        self.critical = if self.critical {
            value > self.critical_clear_mc
        } else {
            value >= self.critical_mc
        };
    }

    pub fn expire(&mut self, now_ms: u64) -> bool {
        if self.value.is_some()
            && self.sampled_ms.is_some_and(|sample| now_ms.saturating_sub(sample) >= STALE_MS)
        {
            self.value = None;
            self.stale = true;
            return true;
        }
        false
    }

    pub fn state(self) -> &'static str {
        match (self.value, self.stale, self.high) {
            (_, true, _) => "stale",
            (None, false, _) => "unavailable",
            (Some(_), false, true) => "high",
            _ => "normal",
        }
    }
}

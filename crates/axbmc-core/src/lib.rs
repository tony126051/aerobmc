#![no_std]

pub mod fan;
pub mod fan_model;

pub mod power;

/// Values and thresholds use the same caller-defined integer unit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Unavailable,
    Normal,
    High,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    InvalidThreshold,
    TimeReversed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Event {
    pub sensor_id: u32,
    pub sequence: u64,
    pub time_ns: u64,
    pub from: State,
    pub to: State,
}

/// Single-owner FIFO. On saturation, retain existing events and count drops.
pub struct EventQueue<const N: usize> {
    slots: [Option<Event>; N],
    head: usize,
    len: usize,
    pub dropped: u64,
}

impl<const N: usize> Default for EventQueue<N> {
    fn default() -> Self {
        Self {
            slots: [None; N],
            head: 0,
            len: 0,
            dropped: 0,
        }
    }
}

impl<const N: usize> EventQueue<N> {
    pub fn push(&mut self, event: Event) -> bool {
        if self.len == N {
            self.dropped = self.dropped.saturating_add(1);
            return false;
        }
        self.slots[(self.head + self.len) % N] = Some(event);
        self.len += 1;
        true
    }

    pub fn pop(&mut self) -> Option<Event> {
        if self.len == 0 {
            return None;
        }
        let event = self.slots[self.head].take();
        self.head = (self.head + 1) % N;
        self.len -= 1;
        event
    }
}

pub struct Sensor {
    id: u32,
    high: i64,
    clear: i64,
    stale_ns: u64,
    last_call: Option<u64>,
    sampled_at: Option<u64>,
    value: Option<i64>,
    state: State,
    sequence: u64,
}

impl Sensor {
    pub fn new(id: u32, high: i64, clear: i64, stale_ns: u64) -> Result<Self, Error> {
        if clear >= high || stale_ns == 0 {
            return Err(Error::InvalidThreshold);
        }
        Ok(Self {
            id,
            high,
            clear,
            stale_ns,
            last_call: None,
            sampled_at: None,
            value: None,
            state: State::Unavailable,
            sequence: 0,
        })
    }

    pub fn state(&self) -> State {
        self.state
    }
    pub fn value(&self) -> Option<i64> {
        self.value
    }

    fn check_time(&mut self, now: u64) -> Result<(), Error> {
        if self.last_call.is_some_and(|last| now < last) {
            return Err(Error::TimeReversed);
        }
        self.last_call = Some(now);
        Ok(())
    }

    fn transition<const N: usize>(&mut self, to: State, now: u64, queue: &mut EventQueue<N>) {
        if to == self.state {
            return;
        }
        self.sequence = self.sequence.saturating_add(1);
        queue.push(Event {
            sensor_id: self.id,
            sequence: self.sequence,
            time_ns: now,
            from: self.state,
            to,
        });
        self.state = to;
    }

    /// None means an explicit read failure, never a zero reading.
    pub fn sample<const N: usize>(
        &mut self,
        now: u64,
        value: Option<i64>,
        queue: &mut EventQueue<N>,
    ) -> Result<(), Error> {
        self.check_time(now)?;
        self.value = value;
        self.sampled_at = value.map(|_| now);
        let next = match value {
            None => State::Unavailable,
            Some(v) if v >= self.high => State::High,
            Some(v) if self.state == State::High && v > self.clear => State::High,
            Some(_) => State::Normal,
        };
        self.transition(next, now, queue);
        Ok(())
    }

    /// Expire at age >= stale_ns; caller must schedule this check.
    pub fn tick<const N: usize>(
        &mut self,
        now: u64,
        queue: &mut EventQueue<N>,
    ) -> Result<(), Error> {
        self.check_time(now)?;
        if self.sampled_at.is_some_and(|at| now - at >= self.stale_ns) {
            self.value = None;
            self.sampled_at = None;
            self.transition(State::Unavailable, now, queue);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hysteresis_failure_and_recovery() {
        let mut s = Sensor::new(1, 80, 75, 100).unwrap();
        let mut q = EventQueue::<8>::default();
        for (t, v) in [
            (0, Some(70)),
            (1, Some(80)),
            (2, Some(79)),
            (3, Some(76)),
            (4, Some(75)),
            (5, None),
            (6, None),
            (7, Some(0)),
        ] {
            s.sample(t, v, &mut q).unwrap();
        }
        let expected = [
            State::Normal,
            State::High,
            State::Normal,
            State::Unavailable,
            State::Normal,
        ];
        for (i, state) in expected.into_iter().enumerate() {
            let e = q.pop().unwrap();
            assert_eq!(e.to, state);
            assert_eq!(e.sequence, i as u64 + 1);
        }
        assert_eq!(q.pop(), None);
        assert_eq!(s.value(), Some(0));
    }

    #[test]
    fn expiry_and_reversed_clock_are_atomic() {
        let mut s = Sensor::new(2, 80, 75, 10).unwrap();
        let mut q = EventQueue::<8>::default();
        s.sample(100, Some(90), &mut q).unwrap();
        s.tick(109, &mut q).unwrap();
        assert_eq!(s.state(), State::High);
        assert_eq!(s.sample(108, None, &mut q), Err(Error::TimeReversed));
        assert_eq!(s.value(), Some(90));
        s.tick(110, &mut q).unwrap();
        assert_eq!(s.value(), None);
        assert_eq!(s.state(), State::Unavailable);
        s.tick(u64::MAX, &mut q).unwrap();
        assert_eq!(q.pop().unwrap().to, State::High);
        assert_eq!(q.pop().unwrap().to, State::Unavailable);
        assert_eq!(q.pop(), None);
    }

    #[test]
    fn overflow_wrap_and_zero_capacity() {
        let mut s = Sensor::new(1, 2, 1, 10).unwrap();
        let mut q = EventQueue::<1>::default();
        s.sample(0, Some(0), &mut q).unwrap();
        s.sample(1, Some(2), &mut q).unwrap();
        assert_eq!(q.dropped, 1);
        assert_eq!(s.state(), State::High);
        assert_eq!(q.pop().unwrap().sequence, 1);
        s.sample(2, None, &mut q).unwrap();
        assert_eq!(q.pop().unwrap().sequence, 3);
        let mut zero = EventQueue::<0>::default();
        s.sample(3, Some(1), &mut zero).unwrap();
        assert_eq!(zero.dropped, 1);
        assert_eq!(zero.pop(), None);
        assert!(matches!(
            Sensor::new(1, 1, 1, 10),
            Err(Error::InvalidThreshold)
        ));
        assert!(matches!(
            Sensor::new(1, 2, 1, 0),
            Err(Error::InvalidThreshold)
        ));
    }
}

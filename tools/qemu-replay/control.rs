use axbmc_core::fan::Fan;
use axbmc_core::power::{Feedback, Power, PowerGoodEdge, PowerState};
use super::{now_ns, sleep_until, PERIOD_NS};

const PERIOD: u128 = PERIOD_NS as u128;
const CYCLES: usize = 48;
const FAN_LOSS_AT: u128 = 5 * PERIOD + PERIOD / 2;
const FAN_TIMEOUT: u128 = 2 * PERIOD;
const POWER_TIMEOUT: u128 = 3 * PERIOD;
const RETRY_DELAY: u128 = 2 * PERIOD;
const MAX_ATTEMPTS: u8 = 3;

fn timestamped(level_high: bool, attempt: u8, captured_ns: u128) -> Feedback {
    Feedback::Timestamped {
        level_high,
        edge: Some(PowerGoodEdge { attempt, captured_ns }),
    }
}

fn power_good_replay() {
    const MS: u128 = 1_000_000;
    let delayed_observation = 45 * MS;
    for (name, feedback, expected) in [
        ("level_delayed", Feedback::Level(true), PowerState::Backoff(delayed_observation + RETRY_DELAY)),
        ("edge_delayed", timestamped(true, 1, 20 * MS), PowerState::On),
        ("edge_at_deadline", timestamped(true, 1, POWER_TIMEOUT), PowerState::Backoff(delayed_observation + RETRY_DELAY)),
        ("edge_after_deadline", timestamped(true, 1, 40 * MS), PowerState::Backoff(delayed_observation + RETRY_DELAY)),
        ("edge_level_low", timestamped(false, 1, 20 * MS), PowerState::Backoff(delayed_observation + RETRY_DELAY)),
        ("edge_future", timestamped(true, 1, 46 * MS), PowerState::Backoff(delayed_observation + RETRY_DELAY)),
    ] {
        let mut power = Power::new(POWER_TIMEOUT, RETRY_DELAY, MAX_ATTEMPTS);
        power.poll(0, Feedback::Level(false));
        power.poll(delayed_observation, feedback);
        assert_eq!(power.state(), expected);
        assert_eq!(power.enabled(), expected == PowerState::On);
        let outcome = if power.enabled() { "on" } else { "timeout" };
        super::println!("BMC_POWER_GOOD,{name},{outcome}");
    }

    let mut power = Power::new(POWER_TIMEOUT, RETRY_DELAY, MAX_ATTEMPTS);
    power.poll(0, Feedback::Level(false));
    power.poll(POWER_TIMEOUT, Feedback::Level(false));
    power.poll(45 * MS, timestamped(true, 1, 20 * MS));
    assert_eq!(power.state(), PowerState::Backoff(65 * MS));
    assert!(!power.enabled());
    super::println!("BMC_POWER_GOOD,edge_after_shutdown,ignored");

    power.poll(65 * MS, Feedback::Level(false));
    power.poll(85 * MS, timestamped(true, 1, 80 * MS));
    assert_eq!(power.state(), PowerState::Waiting(65 * MS));
    super::println!("BMC_POWER_GOOD,edge_old_attempt,ignored");
    power.poll(85 * MS, timestamped(true, 2, 64 * MS));
    assert_eq!(power.state(), PowerState::Waiting(65 * MS));
    super::println!("BMC_POWER_GOOD,edge_before_enable,ignored");
    power.poll(110 * MS, timestamped(true, 2, 85 * MS));
    assert_eq!(power.state(), PowerState::On);
    assert_eq!((power.attempts(), power.timeouts()), (2, 1));
    super::println!("BMC_POWER_GOOD,edge_current_retry,on");
}

fn power_good_irq_replay() {
    const MS: u128 = 1_000_000;
    let dispatch = 45 * MS;
    for (name, captured, timer_first, should_stay_on) in [
        ("irq_before_timer", 20 * MS, false, true),
        ("timer_before_irq", 20 * MS, true, false),
        ("deadline_irq_first", POWER_TIMEOUT, false, false),
        ("deadline_timer_first", POWER_TIMEOUT, true, false),
        ("late_irq_first", 40 * MS, false, false),
        ("late_timer_first", 40 * MS, true, false),
    ] {
        let mut power = Power::new(POWER_TIMEOUT, RETRY_DELAY, MAX_ATTEMPTS);
        power.poll(0, Feedback::Level(false));
        let edge = PowerGoodEdge { attempt: 1, captured_ns: captured };
        if timer_first {
            power.on_timeout(dispatch, 1);
            assert!(!power.enabled());
            power.on_power_good_irq(dispatch, edge, true);
        } else {
            power.on_power_good_irq(dispatch, edge, true);
            power.on_timeout(dispatch, 1);
        }
        // Repeated delivery may neither increment timeout twice nor restart power.
        power.on_timeout(dispatch, 1);
        power.on_power_good_irq(dispatch, edge, true);
        assert_eq!(power.enabled(), should_stay_on);
        assert_eq!(power.timeouts(), u8::from(!should_stay_on));
        let outcome = if should_stay_on {
            assert_eq!(power.state(), PowerState::On);
            "on"
        } else {
            assert_eq!(power.state(), PowerState::Backoff(dispatch + RETRY_DELAY));
            "timeout"
        };
        super::println!("BMC_POWER_GOOD,{name},{outcome}");
    }

    let mut power = Power::new(POWER_TIMEOUT, RETRY_DELAY, MAX_ATTEMPTS);
    power.poll(0, Feedback::Level(false));
    power.on_timeout(38 * MS, 1);
    assert!(power.enabled());
    assert_eq!(power.state(), PowerState::Waiting(0));
    super::println!("BMC_POWER_GOOD,early_timer,ignored");
    power.on_timeout(POWER_TIMEOUT, 1);
    let retry_at = POWER_TIMEOUT + RETRY_DELAY;
    power.poll(retry_at, Feedback::Level(false));
    let retry_deadline = retry_at + POWER_TIMEOUT;
    power.on_timeout(retry_deadline, 1);
    assert_eq!(power.state(), PowerState::Waiting(retry_at));
    assert!(power.enabled());
    super::println!("BMC_POWER_GOOD,old_timer_after_retry,ignored");
    power.on_timeout(retry_deadline, 2);
    assert_eq!(power.timeouts(), 2);
    assert!(!power.enabled());
    super::println!("BMC_POWER_GOOD,current_timer_after_retry,timeout");
}

fn scenario(name: &str, mut step: impl FnMut(u128)) {
    let start = now_ns();
    let mut samples = [(0, 0); CYCLES];
    for (index, sample) in samples.iter_mut().enumerate() {
        let deadline = start + (index as u128 + 1) * PERIOD;
        sleep_until(deadline);
        let woke = now_ns();
        step(woke - start);
        *sample = (woke - deadline, now_ns() - deadline);
    }
    for (index, (wake, finish)) in samples.into_iter().enumerate() {
        super::println!("BMC_CONTROL,{name},{index},{wake},{finish}");
    }
}

pub(super) fn run() {
    power_good_replay();
    power_good_irq_replay();
    let mut fan = Fan::new(50, 100, FAN_TIMEOUT);
    scenario("fan_loss", |now| {
        let rpm = (now < FAN_LOSS_AT).then_some(u16::from(fan.pwm_percent()) * 100);
        fan.poll(now, rpm);
        assert_eq!(fan.pwm_percent(), if fan.protected_at().is_some() { 100 } else { 50 });
    });
    let response = fan.protected_at().unwrap() - FAN_LOSS_AT;
    assert!(response >= FAN_TIMEOUT);
    super::println!("BMC_CONTROL_RESULT,fan_loss,protected,0,0,{response},0");

    for (name, recover) in [("power_retry", true), ("power_absent", false)] {
        let mut power = Power::new(POWER_TIMEOUT, RETRY_DELAY, MAX_ATTEMPTS);
        let mut terminal_at = None;
        let mut first_enable = None;
        scenario(name, |now| {
            let feedback = match power.state() {
                PowerState::Waiting(started) => {
                    recover && power.enabled() && power.attempts() == 2 && now - started >= 20_000_000
                }
                _ => false,
            };
            power.poll(now, Feedback::Level(feedback));
            if power.enabled() {
                first_enable.get_or_insert(now);
            }
            if matches!(power.state(), PowerState::On | PowerState::Failed) {
                terminal_at.get_or_insert(now);
            }
        });
        let (state, attempts, timeouts) = if recover && power.state() == PowerState::On {
            assert_eq!(power.state(), PowerState::On);
            assert!(power.enabled());
            ("on", 2, 1)
        } else {
            // A slow poll can observe the second attempt's feedback after its deadline.
            assert_eq!(power.state(), PowerState::Failed);
            assert!(!power.enabled());
            ("failed", 3, 3)
        };
        assert_eq!((power.attempts(), power.timeouts()), (attempts, timeouts));
        let elapsed = terminal_at.unwrap() - first_enable.unwrap();
        super::println!(
            "BMC_CONTROL_RESULT,{name},{state},{attempts},{timeouts},{elapsed},{}",
            power.worst_timeout_lateness()
        );
    }
}

#![cfg_attr(target_os = "none", no_std)]
#![cfg_attr(target_os = "none", no_main)]

const TIMEOUT_NS: u64 = 100_000_000;
const CHECK_TIMEOUT: bool = cfg!(feature = "timeout");
const CHECK_FAN: bool = cfg!(feature = "fan");

#[cfg(all(feature = "fan", feature = "timeout"))]
compile_error!("fan and timeout are separate QEMU scenarios");

fn verify_fan(
    captured: u64,
    now: impl Fn() -> u64,
    wait_until: impl Fn(u64),
    write_protection: impl FnOnce(),
) -> u64 {
    let mut fan = axbmc_core::fan::Fan::new(50, 100, TIMEOUT_NS.into());
    // This fixture delivers a single tach edge and then no further pulses.
    fan.poll(captured.into(), None);
    let deadline = captured + TIMEOUT_NS;
    wait_until(deadline);
    let fired = now();
    fan.poll(fired.into(), None);
    assert_eq!(fan.protected_at(), Some(fired.into()));
    assert_eq!(fan.pwm_percent(), 100);
    write_protection();
    now()
}

use axbmc_core::power::{Feedback, Power, PowerGoodEdge, PowerState};

fn arm(started: u64) -> Power {
    let mut power = Power::new(TIMEOUT_NS.into(), 0, 1);
    power.poll(started.into(), Feedback::Level(false));
    assert!(power.enabled());
    power
}

fn accept_edge(power: &mut Power, captured: u64, now: u64, level_high: bool) {
    power.on_power_good_irq(
        now.into(),
        PowerGoodEdge { attempt: power.attempts(), captured_ns: captured.into() },
        level_high,
    );
    assert_eq!(power.state(), if CHECK_TIMEOUT { PowerState::Failed } else { PowerState::On });
}

#[cfg(target_os = "none")]
mod arceos {
    use super::{CHECK_FAN, CHECK_TIMEOUT, TIMEOUT_NS, accept_edge, arm, verify_fan};
    use arceos_api::task::{AxWaitQueueHandle, ax_wait_queue_wait_until, ax_wait_queue_wake};
    use core::sync::atomic::{AtomicU64, Ordering};
    use core::time::Duration;

    const GPIO: usize = 0x0903_0000;
    const IRQ: usize = 39;
    static CAPTURED: AtomicU64 = AtomicU64::new(0);
    static FINISHED: AtomicU64 = AtomicU64::new(0);
    static EDGE_READY: AxWaitQueueHandle = AxWaitQueueHandle::new();

    fn now() -> u64 {
        arceos_api::time::ax_monotonic_time().as_nanos().try_into().unwrap()
    }

    // PL061 registers are device MMIO, mapped by the QEMU virt boot page table.
    unsafe fn write(offset: usize, value: u32) {
        let address = axhal::mem::phys_to_virt((GPIO + offset).into()).as_usize();
        unsafe { core::ptr::write_volatile(address as *mut u32, value) };
        unsafe { core::arch::asm!("dsb sy", options(nostack, preserves_flags)) };
    }

    fn handler() {
        let captured = now();
        unsafe {
            write(0x410, 0); // Mask after the first event.
            write(0x41c, 8); // Clear GPIO3 interrupt.
        }
        CAPTURED.store(captured, Ordering::Relaxed);
        FINISHED.store(now(), Ordering::Release);
        ax_wait_queue_wake(&EDGE_READY, 1);
    }

    fn wait_until(deadline: u64) {
        while let Some(remaining) = deadline.checked_sub(now()).filter(|n| *n > 0) {
            axstd::thread::sleep(Duration::from_nanos(remaining));
        }
    }

    pub fn run() {
        unsafe {
            write(0x410, 0);
            write(0x400, 1); // GPIO0 output, GPIO3 input.
            write(0x004, 0);
            write(0x404, 0); // Edge-sensitive.
            write(0x408, 0); // Single edge.
            write(0x40c, 8); // Rising edge GPIO3.
            write(0x41c, 8);
        }
        assert!(axhal::irq::register(IRQ, handler));
        axhal::irq::set_enable(IRQ, true);
        unsafe { write(0x410, 8) };
        if CHECK_FAN {
            axstd::println!("GPIO_READY");
            ax_wait_queue_wait_until(
                &EDGE_READY,
                || FINISHED.load(Ordering::Acquire) != 0,
                Some(Duration::from_secs(5)),
            );
            assert_ne!(FINISHED.load(Ordering::Acquire), 0, "tach IRQ timeout");
            let captured = CAPTURED.load(Ordering::Relaxed);
            let finished = verify_fan(captured, now, wait_until, || unsafe { write(0x004, 1) });
            axstd::println!("GPIO_RESULT,arceos,{captured},{finished},1");
            axstd::println!("GPIO_DONE");
            loop {
                axstd::thread::sleep(Duration::from_secs(1));
            }
        }
        let started = now();
        let mut power = arm(started);
        unsafe { write(0x004, u32::from(power.enabled())) };
        let cutoff = started + TIMEOUT_NS;
        if CHECK_TIMEOUT {
            axstd::println!("GPIO_ARMED");
            while now() < cutoff {
                axstd::thread::sleep(Duration::from_millis(1));
            }
            let fired = now();
            power.on_timeout(fired.into(), power.attempts());
            unsafe { write(0x004, u32::from(power.enabled())) };
            axstd::println!("GPIO_TIMEOUT,{cutoff},{fired},{}", now());
        } else {
            axstd::println!("GPIO_READY");
        }
        ax_wait_queue_wait_until(
            &EDGE_READY,
            || FINISHED.load(Ordering::Acquire) != 0,
            Some(Duration::from_secs(5)),
        );
        assert_ne!(FINISHED.load(Ordering::Acquire), 0, "GPIO IRQ timeout");
        let captured = CAPTURED.load(Ordering::Relaxed);
        let input = axhal::mem::phys_to_virt((GPIO + 0x020).into()).as_usize();
        let level_high = unsafe { core::ptr::read_volatile(input as *const u32) } & 8 != 0;
        accept_edge(&mut power, captured, now(), level_high);
        let output = u8::from(power.enabled());
        unsafe { write(0x004, output.into()) };
        axstd::println!("GPIO_RESULT,arceos,{captured},{},{output}", now());
        if !CHECK_TIMEOUT {
            while let Some(remaining) = cutoff.checked_sub(now()).filter(|n| *n > 0) {
                axstd::thread::sleep(Duration::from_nanos(remaining));
            }
            power.on_timeout(now().into(), power.attempts());
            assert_eq!(power.timeouts(), 0);
            assert!(power.enabled());
            unsafe { write(0x004, u32::from(power.enabled())) };
            axstd::println!("GPIO_CONFIRMED");
        }
        axstd::println!("GPIO_DONE");
        loop {
            axstd::thread::sleep(Duration::from_secs(1));
        }
    }
}

#[cfg(not(target_os = "none"))]
mod linux {
    use super::{CHECK_FAN, CHECK_TIMEOUT, TIMEOUT_NS, accept_edge, arm, verify_fan};
    use std::{fs::{self, File, OpenOptions}, io::Read, os::fd::{AsRawFd, FromRawFd}};

    unsafe extern "C" {
        fn mount(source: *const u8, target: *const u8, fstype: *const u8, flags: usize, data: *const u8) -> i32;
        fn syscall(number: i64, ...) -> i64;
        fn ioctl(fd: i32, request: usize, ...) -> i32;
        fn clock_gettime(clock: i32, time: *mut Timespec) -> i32;
    }

    #[repr(C)]
    struct Timespec { seconds: i64, nanos: i64 }

    #[repr(C)]
    struct HandleRequest {
        offsets: [u32; 64],
        flags: u32,
        values: [u8; 64],
        label: [u8; 32],
        lines: u32,
        fd: i32,
    }

    #[repr(C)]
    struct EventRequest {
        offset: u32,
        flags: u32,
        edges: u32,
        label: [u8; 32],
        fd: i32,
    }

    fn now() -> u64 {
        let mut time = Timespec { seconds: 0, nanos: 0 };
        assert_eq!(unsafe { clock_gettime(1, &mut time) }, 0);
        time.seconds as u64 * 1_000_000_000 + time.nanos as u64
    }

    fn set_output(output: &File, value: u8) {
        let mut values = [0u8; 64];
        values[0] = value;
        assert_eq!(unsafe { ioctl(output.as_raw_fd(), 0xc040b409, &mut values) }, 0);
    }

    fn wait_until(deadline: u64) {
        while let Some(remaining) = deadline.checked_sub(now()).filter(|n| *n > 0) {
            std::thread::sleep(std::time::Duration::from_nanos(remaining));
        }
    }

    fn read_rising_edge(input: &mut File) -> u64 {
        let mut data = [0u8; 16];
        input.read_exact(&mut data).unwrap();
        assert_eq!(u32::from_ne_bytes(data[8..12].try_into().unwrap()), 1);
        u64::from_ne_bytes(data[..8].try_into().unwrap())
    }

    pub fn run() {
        fs::create_dir_all("/dev").unwrap();
        assert_eq!(unsafe { mount(b"devtmpfs\0".as_ptr(), b"/dev\0".as_ptr(),
            b"devtmpfs\0".as_ptr(), 0, core::ptr::null()) }, 0);
        let module = File::open("/gpio-pl061.ko").unwrap();
        assert_eq!(unsafe { syscall(273, module.as_raw_fd(), b"\0".as_ptr(), 0) }, 0);
        let chip = OpenOptions::new().read(true).write(true).open("/dev/gpiochip0").unwrap();
        let mut output = HandleRequest {
            offsets: [0; 64], flags: 2, values: [0; 64], label: [0; 32], lines: 1, fd: -1,
        };
        assert_eq!(core::mem::size_of::<HandleRequest>(), 364);
        assert_eq!(unsafe { ioctl(chip.as_raw_fd(), 0xc16cb403, &mut output) }, 0);
        let output_fd = unsafe { File::from_raw_fd(output.fd) };
        let mut event = EventRequest { offset: 3, flags: 1, edges: 1, label: [0; 32], fd: -1 };
        assert_eq!(core::mem::size_of::<EventRequest>(), 48);
        assert_eq!(unsafe { ioctl(chip.as_raw_fd(), 0xc030b404, &mut event) }, 0);
        let mut input = unsafe { File::from_raw_fd(event.fd) };
        if CHECK_FAN {
            println!("GPIO_READY");
            let captured = read_rising_edge(&mut input);
            let finished = verify_fan(captured, now, wait_until, || set_output(&output_fd, 1));
            println!("GPIO_RESULT,linux,{captured},{finished},1");
            println!("GPIO_DONE");
            loop {
                std::thread::park();
            }
        }
        let started = now();
        let mut power = arm(started);
        set_output(&output_fd, u8::from(power.enabled()));
        let cutoff = started + TIMEOUT_NS;
        if CHECK_TIMEOUT {
            println!("GPIO_ARMED");
            while let Some(remaining) = cutoff.checked_sub(now()).filter(|n| *n > 0) {
                std::thread::sleep(std::time::Duration::from_nanos(remaining));
            }
            let fired = now();
            power.on_timeout(fired.into(), power.attempts());
            set_output(&output_fd, u8::from(power.enabled()));
            println!("GPIO_TIMEOUT,{cutoff},{fired},{}", now());
        } else {
            println!("GPIO_READY");
        }
        let captured = read_rising_edge(&mut input);
        let mut values = [0u8; 64];
        assert_eq!(unsafe { ioctl(input.as_raw_fd(), 0xc040b408, &mut values) }, 0);
        accept_edge(&mut power, captured, now(), values[0] != 0);
        let output = u8::from(power.enabled());
        set_output(&output_fd, output);
        let finished = now();
        assert!(finished >= captured);
        println!("GPIO_RESULT,linux,{captured},{finished},{output}");
        if !CHECK_TIMEOUT {
            while let Some(remaining) = cutoff.checked_sub(now()).filter(|n| *n > 0) {
                std::thread::sleep(std::time::Duration::from_nanos(remaining));
            }
            power.on_timeout(now().into(), power.attempts());
            assert_eq!(power.timeouts(), 0);
            assert!(power.enabled());
            set_output(&output_fd, u8::from(power.enabled()));
            println!("GPIO_CONFIRMED");
        }
        println!("GPIO_DONE");
        // Retain line ownership until the host verifies the output register.
        loop { std::thread::park(); }
    }
}

#[cfg_attr(target_os = "none", unsafe(no_mangle))]
fn main() {
    #[cfg(target_os = "none")]
    arceos::run();
    #[cfg(not(target_os = "none"))]
    linux::run();
}

//! QEMU virt PL061 adapter. GPIO0 enables power, GPIO1 selects protection, GPIO2 selects thermal boost,
//! GPIO4 receives tach, GPIO5 power-good and GPIO6 host heartbeat.
use crate::controller::{Controller, Input, Power};

fn rpm(previous: u64, captured: u64) -> Option<u32> {
    let period = captured.checked_sub(previous).filter(|period| *period > 0)?;
    u32::try_from(30_000_000_000 / period).ok().filter(|rpm| *rpm > 0)
}

fn outputs(state: &Controller) -> [u8; 3] {
    [
        u8::from(matches!(state.power, Power::Starting { .. } | Power::On)),
        u8::from(state.fan_duty() == 100),
        u8::from(state.fan_duty() == 75),
    ]
}

#[cfg(target_os = "none")]
mod platform {
    use super::{outputs, rpm, Controller, Input};
    use core::sync::atomic::{AtomicU32, AtomicU64, Ordering};

    const BASE: usize = 0x0903_0000;
    const TACH: u32 = 1 << 4;
    const GOOD: u32 = 1 << 5;
    const HEARTBEAT: u32 = 1 << 6;
    static LAST_TACH: AtomicU64 = AtomicU64::new(0);
    static RPM: AtomicU32 = AtomicU32::new(0);
    static POWER_GOOD: AtomicU64 = AtomicU64::new(0);
    static HEARTBEAT_EDGE: AtomicU64 = AtomicU64::new(0);

    fn register(offset: usize) -> *mut u32 {
        axhal::mem::phys_to_virt((BASE + offset).into()).as_usize() as *mut u32
    }

    fn write(offset: usize, value: u32) {
        unsafe {
            register(offset).write_volatile(value);
            core::arch::asm!("dsb sy", options(nostack, preserves_flags));
        }
    }

    fn read(offset: usize) -> u32 {
        unsafe { register(offset).read_volatile() }
    }

    fn interrupt() {
        let pending = read(0x418) & (TACH | GOOD | HEARTBEAT);
        write(0x41c, pending);
        if pending & TACH != 0 {
            let captured = arceos_api::time::ax_monotonic_time().as_nanos() as u64;
            let previous = LAST_TACH.swap(captured, Ordering::Relaxed);
            if previous != 0 {
                if let Some(value) = rpm(previous, captured) {
                    RPM.store(value, Ordering::Release);
                }
            }
        }
        if pending & GOOD != 0 {
            POWER_GOOD.store(arceos_api::time::ax_monotonic_time().as_nanos() as u64, Ordering::Release);
        }
        if pending & HEARTBEAT != 0 {
            HEARTBEAT_EDGE.store(arceos_api::time::ax_monotonic_time().as_nanos() as u64, Ordering::Release);
        }
    }

    pub struct Device;

    impl Device {
        pub fn open() -> Self {
            use axhal::paging::MappingFlags;
            let physical = BASE.into();
            axmm::kernel_aspace().lock().map_linear(
                axhal::mem::phys_to_virt(physical),
                physical,
                4096,
                MappingFlags::READ | MappingFlags::WRITE | MappingFlags::DEVICE,
            ).expect("map PL061");
            write(0x410, 0);
            write(0x400, 7);
            write(0x01c, 0);
            write(0x404, 0);
            write(0x408, 0);
            write(0x40c, TACH | GOOD | HEARTBEAT);
            write(0x41c, TACH | GOOD | HEARTBEAT);
            assert!(axhal::irq::register(39, interrupt));
            axhal::irq::set_enable(39, true);
            write(0x410, TACH | GOOD | HEARTBEAT);
            Self
        }

        pub fn drain(&mut self, state: &mut Controller, start: u64) {
            let captured = POWER_GOOD.swap(0, Ordering::AcqRel);
            let heartbeat = HEARTBEAT_EDGE.swap(0, Ordering::AcqRel);
            let now = crate::millis().saturating_sub(start);
            if captured != 0 || state.power == super::Power::On {
                let high = read(0x080) & GOOD != 0;
                state.observe_power_good(now, high);
                if captured != 0 && high {
                    state.apply(now, Input::PowerGoodAt((captured / 1_000_000).saturating_sub(start)));
                }
            }
            if heartbeat != 0 {
                state.apply(now, Input::HeartbeatAt((heartbeat / 1_000_000).saturating_sub(start)));
            }
            let value = RPM.swap(0, Ordering::AcqRel);
            if value != 0 {
                state.apply(now, Input::Tach(value));
            }
        }

        pub fn publish(&self, state: &Controller) {
            let [power, fan, boost] = outputs(state);
            write(0x01c, u32::from(power) | (u32::from(fan) << 1) | (u32::from(boost) << 2));
        }
    }
}

#[cfg(all(target_os = "linux", target_arch = "aarch64"))]
mod platform {
    use super::{outputs, rpm, Controller, Input};
    use std::fs::{File, OpenOptions};
    use std::io::{self, Read};
    use std::os::fd::{AsRawFd, FromRawFd};

    #[repr(C)]
    struct Timespec {
        seconds: i64,
        nanos: i64,
    }

    pub fn monotonic_ms() -> u64 {
        let mut value = Timespec { seconds: 0, nanos: 0 };
        assert_eq!(unsafe { clock_gettime(1, &mut value) }, 0);
        value.seconds as u64 * 1000 + value.nanos as u64 / 1_000_000
    }

    unsafe extern "C" {
        fn clock_gettime(clock: i32, value: *mut Timespec) -> i32;
        fn ioctl(fd: i32, request: usize, ...) -> i32;
        fn fcntl(fd: i32, command: i32, ...) -> i32;
    }

    #[repr(C)]
    struct Lines {
        offsets: [u32; 64],
        flags: u32,
        values: [u8; 64],
        label: [u8; 32],
        count: u32,
        fd: i32,
    }

    #[repr(C)]
    struct Edge {
        offset: u32,
        flags: u32,
        edges: u32,
        label: [u8; 32],
        fd: i32,
    }

    fn check(result: i32) -> io::Result<()> {
        if result < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
    }

    fn open_edge(chip: &File, offset: u32) -> io::Result<File> {
        let mut edge = Edge { offset, flags: 1, edges: 1, label: [0; 32], fd: -1 };
        check(unsafe { ioctl(chip.as_raw_fd(), 0xc030b404, &mut edge) })?;
        let fd = unsafe { File::from_raw_fd(edge.fd) };
        let flags = unsafe { fcntl(fd.as_raw_fd(), 3) };
        check(flags)?;
        check(unsafe { fcntl(fd.as_raw_fd(), 4, flags | 0x800) })?;
        Ok(fd)
    }

    fn next_edge(fd: &mut File) -> Option<u64> {
        let mut event = [0u8; 16];
        match fd.read(&mut event) {
            Ok(16) => Some(u64::from_ne_bytes(event[..8].try_into().unwrap())),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => None,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => None,
            other => panic!("invalid GPIO event: {other:?}"),
        }
    }

    pub struct Device {
        output: File,
        tach: File,
        power_good: File,
        heartbeat: File,
        previous_tach: Option<u64>,
    }

    impl Device {
        pub fn open() -> io::Result<Self> {
            let chip = OpenOptions::new().read(true).write(true).open("/dev/gpiochip0")?;
            let mut lines = Lines {
                offsets: [0; 64], flags: 2, values: [0; 64], label: [0; 32], count: 3, fd: -1,
            };
            lines.offsets[1] = 1;
            lines.offsets[2] = 2;
            check(unsafe { ioctl(chip.as_raw_fd(), 0xc16cb403, &mut lines) })?;
            let output = unsafe { File::from_raw_fd(lines.fd) };
            Ok(Self {
                output,
                tach: open_edge(&chip, 4)?,
                power_good: open_edge(&chip, 5)?,
                heartbeat: open_edge(&chip, 6)?,
                previous_tach: None,
            })
        }

        pub fn drain(&mut self, state: &mut Controller, start: u64) {
            if state.power == super::Power::On {
                let high = self.power_good_high();
                state.observe_power_good(monotonic_ms().saturating_sub(start), high);
            }
            while let Some(captured) = next_edge(&mut self.power_good) {
                if self.power_good_high() {
                    state.apply(monotonic_ms().saturating_sub(start),
                        Input::PowerGoodAt((captured / 1_000_000).saturating_sub(start)));
                }
            }
            while let Some(captured) = next_edge(&mut self.heartbeat) {
                state.apply(monotonic_ms().saturating_sub(start),
                    Input::HeartbeatAt((captured / 1_000_000).saturating_sub(start)));
            }
            while let Some(captured) = next_edge(&mut self.tach) {
                if let Some(previous) = self.previous_tach.replace(captured) {
                    if let Some(value) = rpm(previous, captured) {
                        state.apply(monotonic_ms().saturating_sub(start), Input::Tach(value));
                    }
                }
            }
        }

        fn power_good_high(&self) -> bool {
            let mut values = [0u8; 64];
            check(unsafe { ioctl(self.power_good.as_raw_fd(), 0xc040b408, &mut values) })
                .expect("read power-good level");
            values[0] != 0
        }

        pub fn publish(&self, state: &Controller) {
            let mut values = [0u8; 64];
            values[..3].copy_from_slice(&outputs(state));
            check(unsafe { ioctl(self.output.as_raw_fd(), 0xc040b409, &mut values) })
                .expect("write GPIO outputs");
        }
    }
}

pub use platform::Device;

#[cfg(all(target_os = "linux", target_arch = "aarch64"))]
pub use platform::monotonic_ms;

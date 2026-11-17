#!/bin/sh
set -eu
if [ "$#" -ne 6 ]; then
    echo "Usage: $0 DISPOSABLE_PLATFORM_CHECKOUT KERNEL MODULE_APK KERNEL_RELEASE BUSYBOX_APK NEW_OUTPUT" >&2
    exit 2
fi
project=$(cd "$(dirname "$0")/.." && pwd)
platform=$(cd "$1/arceos" && pwd)
kernel=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
modules=$3
release=$4
busybox=$5
mkdir "$6"
output=$(cd "$6" && pwd)
ramfs="$output/root"
app="$platform/examples/bmc-runtime"
test ! -e "$app"
mkdir -p "$ramfs" "$app/src"
cp "$project"/src/*.rs "$app/src/"
cat > "$app/Cargo.toml" <<'TOML'
[package]
name = "bmc-runtime"
version = "0.1.0"
edition = "2021"

[dependencies]
axstd.workspace = true
arceos_api.workspace = true
axhal.workspace = true
axconfig.workspace = true
axmm.workspace = true
serde_json = { version = "=1.0.149", default-features = false, features = ["alloc"] }
TOML
python3 - "$platform" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
manifest = root / "Cargo.toml"
text = manifest.read_text()
anchor = "members = [\n"
assert text.count(anchor) == 1
manifest.write_text(text.replace(anchor, anchor + '    "examples/bmc-runtime",\n', 1))
# Rust 2024 requires unsafe FFI declarations in the pinned platform fork.
paging = root / "modules/axmm/src/vmsa_uma.rs"
text = paging.read_text()
old = '        extern "C" {\n            fn early_page_alloc()'
if old in text:
    paging.write_text(text.replace(old, old.replace("extern", "unsafe extern", 1), 1))
# The pinned smoltcp adapter removes a closing socket immediately. Give queued
# bytes a bounded chance to reach the peer before Drop removes that socket.
tcp = root / "modules/axnet/src/smoltcp_impl/tcp.rs"
text = tcp.read_text()
old = """            unsafe { self.local_addr.get().write(UNSPECIFIED_ENDPOINT) }; // clear bound address
            SOCKET_SET.poll_interfaces();
            Ok(())"""
new = """            unsafe { self.local_addr.get().write(UNSPECIFIED_ENDPOINT) }; // clear bound address
            let deadline = axhal::time::monotonic_time() + core::time::Duration::from_millis(200);
            loop {
                SOCKET_SET.poll_interfaces();
                let pending = SOCKET_SET.with_socket_mut::<tcp::Socket, _, _>(
                    handle, |socket| socket.send_queue(),
                );
                if pending == 0 || axhal::time::monotonic_time() >= deadline {
                    break;
                }
                axtask::sleep(core::time::Duration::from_millis(1));
            }
            Ok(())"""
assert old in text, "unexpected platform TCP shutdown implementation"
text = text.replace(old, new, 1)
# Keep polling smoltcp while allowing the guest to idle between polls.
old = """            loop {
                SOCKET_SET.poll_interfaces();
                match f() {
                    Ok(t) => return Ok(t),
                    Err(AxError::WouldBlock) => axtask::yield_now(),"""
new = """            let deadline = axhal::time::monotonic_time() + core::time::Duration::from_secs(1);
            loop {
                SOCKET_SET.poll_interfaces();
                match f() {
                    Ok(t) => return Ok(t),
                    Err(AxError::WouldBlock) if axhal::time::monotonic_time() >= deadline =>
                        return Err(AxError::WouldBlock),
                    Err(AxError::WouldBlock) => axtask::sleep(core::time::Duration::from_millis(1)),"""
assert text.count(old) == 1, "unexpected platform TCP wait implementation"
tcp.write_text(text.replace(old, new, 1))
listen = root / "modules/axnet/src/smoltcp_impl/listen_table.rs"
text = listen.read_text()
old = "            let syn_queue = &mut entry.syn_queue;"
assert text.count(old) == 1
text = text.replace(old, old + """
            syn_queue.retain(|&handle| {
                let closed = SOCKET_SET.with_socket::<tcp::Socket, _, _>(
                    handle, |socket| socket.state() == State::Closed,
                );
                if closed {
                    SOCKET_SET.remove(handle);
                }
                !closed
            });""", 1)
old = "                    is_connected(handle).then(|| (idx, get_addr_tuple(handle)))"
assert text.count(old) == 1
text = text.replace(old, "                    get_addr_tuple(handle).map(|endpoints| (idx, endpoints))", 1)
old = """        !matches!(socket.state(), State::Listen | State::SynReceived)
    })
}

fn get_addr_tuple(handle: SocketHandle) -> (IpEndpoint, IpEndpoint) {
    SOCKET_SET.with_socket::<tcp::Socket, _, _>(handle, |socket| {
        (
            socket.local_endpoint().unwrap(),
            socket.remote_endpoint().unwrap(),
        )
    })
}"""
new = """        socket_endpoints(socket).is_some()
    })
}

fn get_addr_tuple(handle: SocketHandle) -> Option<(IpEndpoint, IpEndpoint)> {
    SOCKET_SET.with_socket::<tcp::Socket, _, _>(handle, socket_endpoints)
}

fn socket_endpoints(socket: &tcp::Socket<'_>) -> Option<(IpEndpoint, IpEndpoint)> {
    if matches!(socket.state(), State::Listen | State::SynReceived | State::Closed) {
        return None;
    }
    Some((socket.local_endpoint()?, socket.remote_endpoint()?))
}"""
assert text.count(old) == 1
listen.write_text(text.replace(old, new, 1))
config = root / "configs/defconfig.toml"
text = config.read_text()
old = "ticks-per-sec = 100"
assert text.count(old) == 1, "unexpected platform timer configuration"
config.write_text(text.replace(old, "ticks-per-sec = 1000", 1))
PY
export RUSTUP_TOOLCHAIN=nightly-2025-05-20
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld cargo build \
    --manifest-path "$project/Cargo.toml" --release --target aarch64-unknown-linux-musl \
    --target-dir "$output/linux-target" > "$output/linux-build.log" 2>&1
cp "$output/linux-target/aarch64-unknown-linux-musl/release/bmc-runtime" "$ramfs/bmc-runtime"
tar -xOf "$busybox" bin/busybox.static > "$ramfs/busybox"
cp "$project/qemu/init" "$ramfs/init"
chmod +x "$ramfs/init" "$ramfs/busybox"
for module in net/core/failover drivers/net/net_failover drivers/virtio/virtio_mmio drivers/net/virtio_net drivers/gpio/gpio-pl061; do
    tar -xOf "$modules" "lib/modules/$release/kernel/$module.ko.gz" > "$output/module.gz"
    gzip -dc "$output/module.gz" > "$ramfs/${module##*/}.ko"
done
(
    cd "$ramfs"
    printf '%s\n' init busybox bmc-runtime failover.ko net_failover.ko virtio_mmio.ko virtio_net.ko gpio-pl061.ko |
        cpio -o -H newc > "$output/linux.cpio"
)
(
    cd "$platform"
    make A=examples/bmc-runtime ARCH=aarch64 SMP=1 MEM=256M LOG=warn \
        FEATURES=alloc,irq,multitask,sched-rr,net BUS=mmio NET=y \
        OUT_CONFIG="$output/axconfig.toml" build
) > "$output/arceos-build.log" 2>&1
cp "$app/bmc-runtime_aarch64-qemu-virt.bin" "$output/arceos.bin"
git -C "$platform" rev-parse HEAD > "$output/platform-commit.txt"
if [ "${BMC_BUILD_ONLY:-0}" != 1 ]; then
    python3 "$project/qemu/verify.py" "${QEMU_BIN:-qemu-system-aarch64}" "$kernel" "$output"
fi

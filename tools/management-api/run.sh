#!/bin/sh
set -eu

if [ "$#" -ne 6 ]; then
    echo "Usage: $0 DISPOSABLE_G1 LINUX_KERNEL LINUX_VIRT_APK KERNEL_RELEASE BUSYBOX_STATIC_APK NEW_OUTPUT" >&2
    exit 2
fi

tools_dir=$(cd "$(dirname "$0")" && pwd)
project=$(cd "$tools_dir/../.." && pwd)
checkout=$(cd "$1" && pwd)
kernel=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
modules=$3
release=$4
busybox=$5
mkdir "$6"
output=$(cd "$6" && pwd)
ramfs="$output/ramfs"
mkdir "$ramfs"
sh "$tools_dir/stage.sh" "$checkout"
export RUSTUP_TOOLCHAIN=nightly-2025-05-20

tar -xOf "$busybox" bin/busybox.static > "$ramfs/busybox"
chmod +x "$ramfs/busybox"
for module in net/core/failover drivers/net/net_failover drivers/virtio/virtio_mmio drivers/net/virtio_net; do
    # Extract first so an archive error is not hidden by the decompressor.
    tar -xOf "$modules" "lib/modules/$release/kernel/$module.ko.gz" > "$ramfs/module.gz"
    gzip -dc "$ramfs/module.gz" > "$ramfs/${module##*/}.ko"
done
rm "$ramfs/module.gz"
cp "$tools_dir/init" "$ramfs/init"
chmod +x "$ramfs/init"

(
    cd "$project"
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
        cargo build -p bmc-management-api --release --target aarch64-unknown-linux-musl \
        --target-dir "$output/linux-target"
) > "$output/linux-build.log" 2>&1
cp "$output/linux-target/aarch64-unknown-linux-musl/release/bmc-management-api" "$ramfs/management-api"
(
    cd "$ramfs"
    printf '%s\n' init busybox failover.ko net_failover.ko virtio_mmio.ko virtio_net.ko management-api |
        cpio -o -H newc > "$output/linux.cpio"
)

(
    cd "$checkout/arceos"
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 make A=examples/bmc-management-api ARCH=aarch64 \
        SMP=1 MEM=256M LOG=warn FEATURES=alloc,irq,multitask,sched-rr,net BUS=mmio NET=y \
        OUT_CONFIG="$output/axconfig.toml" build
) > "$output/arceos-build.log" 2>&1
cp "$checkout/arceos/examples/bmc-management-api/bmc-management-api_aarch64-qemu-virt.bin" "$output/arceos.bin"
git -C "$checkout" rev-parse HEAD > "$output/arceos-commit.txt"
rustc --version > "$output/rustc-version.txt"
python3 "$tools_dir/verify.py" --qemu "${QEMU_BIN:-qemu-system-aarch64}" \
    --kernel "$kernel" --initramfs "$output/linux.cpio" --arceos-image "$output/arceos.bin" \
    --output "$output/verification"

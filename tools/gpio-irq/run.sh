#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
    echo "Usage: $0 DISPOSABLE_G1_CHECKOUT LINUX_KERNEL GPIO_PL061_KO NEW_OUTPUT_DIR" >&2
    exit 2
fi

tools_dir=$(cd "$(dirname "$0")" && pwd)
checkout=$(cd "$1" && pwd)
kernel=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
module=$(cd "$(dirname "$3")" && pwd)/$(basename "$3")
test -f "$kernel"
test -f "$module"
mkdir "$4"
output=$(cd "$4" && pwd)
mkdir "$output/initramfs"
sh "$tools_dir/stage.sh" "$checkout" "$output/linux-app"
cp "$module" "$output/initramfs/gpio-pl061.ko"

export RUSTUP_TOOLCHAIN=nightly-2025-05-20
git -C "$checkout" rev-parse HEAD > "$output/arceos-commit.txt"
rustc --version > "$output/rustc-version.txt"

for mode in edge timeout fan; do
    feature=
    case_option=
    if [ "$mode" = timeout ]; then
        feature=timeout
        case_option=--timeout-case
    elif [ "$mode" = fan ]; then
        feature=fan
        case_option=--fan-case
    fi
    (
        cd "$output/linux-app"
        CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
            cargo build --release --target aarch64-unknown-linux-musl --features "$feature"
    ) > "$output/linux-$mode-build.log" 2>&1
    cp "$output/linux-app/target/aarch64-unknown-linux-musl/release/bmc-gpio-irq" "$output/initramfs/init"
    (
        cd "$output/initramfs"
        printf '%s\n' init gpio-pl061.ko | cpio -o -H newc > "$output/$mode.cpio"
    )
    (
        cd "$checkout/arceos"
        CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 make A=examples/bmc-gpio-irq ARCH=aarch64 \
            SMP=1 MEM=256M LOG=warn FEATURES=irq,multitask,sched-rr \
            APP_FEATURES="$feature" OUT_CONFIG="$output/axconfig.toml" build
    ) > "$output/arceos-$mode-build.log" 2>&1
    cp "$checkout/arceos/examples/bmc-gpio-irq/bmc-gpio-irq_aarch64-qemu-virt.bin" "$output/$mode.bin"
    # The optional flag is a single word, or absent in the edge case.
    python3 "$tools_dir/verify.py" --qemu "${QEMU_BIN:-qemu-system-aarch64}" \
        --kernel "$kernel" --initramfs "$output/$mode.cpio" --arceos-image "$output/$mode.bin" \
        --output "$output/$mode" ${case_option:+$case_option}
done

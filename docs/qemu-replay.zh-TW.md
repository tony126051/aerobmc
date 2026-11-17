# Linux 與 ArceOS 在 AArch64 QEMU 的感測器重播

本頁核對 AeroBMC 最小可行核心在 Linux 與 ArceOS 模擬環境的感測器事件一致性。 [專案定位](../README.md)

2026-11-11。Linux 和 ArceOS 各獨立開機三次。每次都輸出相同的六個事件，序號、邏輯時間、感測器 ID 與前後狀態逐行符合 `traces/sensor.expected.csv`。

| 輸入 | Linux 中位數（範圍） | ArceOS 中位數（範圍） | ArceOS 耗時減少 |
|---|---:|---:|---:|
| 穩定 70°C | 0.784 ms（0.726–0.856） | 0.560 ms（0.518–0.600） | 28.6% |
| 80°C／75°C 交替 | 1.975 ms（1.903–2.059） | 1.249 ms（1.229–1.323） | 36.8% |
| 讀取失敗／70°C 交替 | 1.915 ms（1.829–1.978） | 1.234 ms（1.222–1.296） | 35.6% |

每批 100,000 次更新，包含感測器狀態更新與事件入列、出列。每次開機、每種輸入先執行一批預熱，再收集七批，每列每端共 21 筆。逐批核對事件數並確認佇列無丟棄。CSV 解析、UART 輸出和裝置 I/O 均在計時範圍之外。Linux 使用 `std::time::Instant`，ArceOS 使用 `ax_monotonic_time`。

測試主機是 Apple M5，QEMU 10.2.2 使用 TCG 模擬 `virt,gic-version=2`、Cortex-A72、1 vCPU、256 MiB RAM。Linux 使用 Alpine 6.18.60-0-virt，重播程式作為 initramfs 的 `/init`，採 `aarch64-unknown-linux-musl`。ArceOS 使用 AeroTensor-G1 的 `39220889fae5184885778feb222afa30be18c986`，採 `aarch64-unknown-none-softfloat`。兩端使用同一份重播程式、核心、Rust nightly-2025-05-20、release LTO 和單一 codegen unit。[AeroTensor-G1 commit](https://github.com/tony126051/aerotensorg1/commit/39220889fae5184885778feb222afa30be18c986) · [QEMU Arm 系統模擬文件](https://www.qemu.org/docs/master/system/target-arm.html) · [Quicksand QEMU 套件](https://pypi.org/project/quicksand-qemu/0.5.12/)

這一輪的差距涵蓋編譯目標、guest 計時路徑及 TCG 行為，無法將差距單獨歸因於 OS。下一輪使用同一實體 AArch64 板卡測事件端到端延遲、CPU 與記憶體，再評估推論負載下的 p99。

本機原始資料位於 `results/qemu-aarch64/`，含逐批 `timings.csv`、六份序列埠 log、QEMU 命令及映像／trace SHA-256。核心三項測試與 AeroTensor-G1 的 standalone 驗證皆通過。

## 重現

目前的重播程式也執行週期延遲測試。以下建置啟用 ArceOS 的 round-robin 排程與計時器中斷，對應週期測試環境。本頁上方的首輪批次數據使用當時的單任務組態。

從 BMC-G1 根目錄執行。需有 `qemu-system-aarch64`、Python 3、GNU Make、cpio、Rustup，以及下列建置工具。請使用獨立的 AeroTensor-G1 checkout，因為 staging 會加入暫用的 workspace member。

```sh
rustup toolchain install nightly-2025-05-20 --profile minimal \
  -c rust-src,llvm-tools \
  -t aarch64-unknown-none-softfloat,aarch64-unknown-linux-musl
cargo +stable install --locked cargo-binutils@0.4.0 axconfig-gen@0.2.1 cargo-axplat@0.4.10

run_dir=$(mktemp -d)
git clone https://github.com/tony126051/aerotensorg1 "$run_dir/g1"
git -C "$run_dir/g1" checkout --detach 39220889fae5184885778feb222afa30be18c986
sh tools/qemu-replay/stage.sh "$run_dir/g1" "$run_dir/linux-app"

(
  cd "$run_dir/g1/arceos"
  RUSTUP_TOOLCHAIN=nightly-2025-05-20 CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 \
    make A=examples/bmc-qemu-replay ARCH=aarch64 SMP=1 MEM=256M LOG=off \
    FEATURES=multitask,sched-rr build
)
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=rust-lld \
  cargo +nightly-2025-05-20 build --manifest-path "$run_dir/linux-app/Cargo.toml" \
  --release --target aarch64-unknown-linux-musl
mkdir "$run_dir/initramfs"
cp "$run_dir/linux-app/target/aarch64-unknown-linux-musl/release/bmc-qemu-replay" \
  "$run_dir/initramfs/init"
(cd "$run_dir/initramfs" && printf '%s\n' init | cpio -o -H newc > ../initramfs.cpio)

curl -fL https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/aarch64/netboot/vmlinuz-virt \
  -o "$run_dir/vmlinuz-virt"
python3 tools/qemu-replay/compare.py \
  --linux-kernel "$run_dir/vmlinuz-virt" \
  --initramfs "$run_dir/initramfs.cpio" \
  --arceos-image "$run_dir/g1/arceos/examples/bmc-qemu-replay/bmc-qemu-replay_aarch64-qemu-virt.bin" \
  --output "$run_dir/results"
```

可用 `--qemu` 指定 QEMU 執行檔。Alpine 的下載路徑會隨 v3.23 更新，本輪 kernel 的 SHA-256 為 `4ac63d06e6cf5820905ffa7741292cfbe90fb0cf1edba4c42e871f6daf699437`。

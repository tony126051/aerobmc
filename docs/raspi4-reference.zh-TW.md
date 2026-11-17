# Raspberry Pi 4 Model B 參考公板

專案驗收採全軟體模擬。本頁的 Pi 4B 映像用於 QEMU `raspi4b` 開機驗證，治具與實板開機內容保留為先前的設計紀錄。

Pi 4B 是 BMC-G1 的第一個參考公板。已完成 ArceOS 核心映像、Linux AArch64 重播程式與測試治具腳位草案。實板 revision、韌體、電氣與驅動驗證待取得板卡後完成。

## 映像驗證

- ArceOS 以 AeroTensor-G1 `39220889fae5184885778feb222afa30be18c986`、`axplat-aarch64-raspi` 0.4.1、Rust nightly-2025-05-20 建置。使用一個工作核心、round-robin 與預設 100 Hz 計時器。首次嘗試的頁配置器容量不足，啟用 `page-alloc-4g` 後通過。
- QEMU 10.2.2 `raspi4b` 以 TCG 直接載入核心，通過六筆感測器事件、十個 power-good 案例及三個情境共 144 個控制週期。控制案例使用模擬回饋。實體 Pi 韌體開機、GPIO、PWM、tach 與 IRQ 仍待驗證。
- Linux 檔案是 AArch64 靜態重播程式，可搭配 64 位元 Linux 使用。它使用與 ArceOS 相同的感測器 trace 及控制案例。

本機產物位於 `results/raspi4-reference/`，包含 `kernel8.img`、`bmc-replay-linux-aarch64`、`SHA256SUMS`、`axconfig.toml` 及開機 log。

## 測試治具腳位草案

以下腳位參考官方 J8 接頭表和 BCM2711 pinmux。功能分配由本專案選定，電源使能與 power-good 均接外部 3.3 V 測試治具。電源使能只當治具的邏輯輸入，治具產生準時、遲到與缺失的 power-good 回饋。實際風扇接入前，須依風扇料號確認 PWM／tach 電氣介面及供電。GPIO 接頭採 3.3 V 邏輯，接不同電壓的訊號時使用符合規格的介面電路。[Pi 4 Model B J8 接頭表](https://pip-assets.raspberrypi.com/categories/545-raspberry-pi-4-model-b/documents/RP-008341-DS-1-raspberry-pi-4-datasheet.pdf?disposition=inline) · [BCM2711 pinmux 與 PWM](https://datasheets.raspberrypi.com/bcm2711/bcm2711-peripherals.pdf)

| 治具訊號 | Pi 4B BCM GPIO | J8 實體腳 | 方向與模式 |
|---|---|---:|---|
| PWM | GPIO18 | 12 | 輸出，ALT5 `PWM0_0` |
| TACH | GPIO23 | 16 | 輸入，計數治具脈波。實際風扇每轉脈波數依料號 |
| POWER_ENABLE | GPIO24 | 18 | 輸出，治具規約為高有效、啟動時維持低 |
| POWER_GOOD | GPIO25 | 22 | 輸入，治具規約為高有效。驗證上升緣 IRQ、時間戳與當下電位 |
| UART TX／RX | GPIO14／GPIO15 | 8／10 | PL011 console，配合韌體 pinmux |
| GND | — | 6 或 14 | 與治具及 USB UART 共地 |

Pi 平台的現有 MMIO 設定涵蓋 UART、eMMC 與 GIC。GPIO／PWM 實體後端還需要板級 MMIO 映射、時鐘與 pinmux 設定，以及 IRQ 路由與清除。測試治具先由邏輯分析儀比對上升緣和軟體時間戳。板上 IRQ 處理讀出的時間含中斷延遲。

## 重現建置與實板開機

依 [工具鏈與 staging 步驟](qemu-replay.zh-TW.md) 準備暫用 G1 checkout。進入該 checkout 的 `arceos` 目錄，執行：

```sh
RUSTUP_TOOLCHAIN=nightly-2025-05-20 CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 \
  make A=examples/bmc-qemu-replay ARCH=aarch64 MYPLAT=axplat-aarch64-raspi \
    APP_FEATURES=raspi4 FEATURES=multitask,sched-rr,page-alloc-4g \
    SMP=1 BUS=mmio LOG=warn build
```

輸出為 `examples/bmc-qemu-replay/bmc-qemu-replay_aarch64-raspi4.bin`。若先前在同一 checkout 建過 QEMU virt，請以 `OUT_CONFIG` 指向一個新的 Pi 4 設定檔路徑。QEMU 開機使用：

```sh
qemu-system-aarch64 -M raspi4b -accel tcg -display none -monitor none \
  -serial stdio -serial null -no-reboot -kernel kernel8.img
```

拿到板卡後先用獨立 microSD 準備 Pi 4 韌體與 boot 分割區。將核心存成 `bmc-arceos.img`，由 `config.txt` 選擇 `kernel=bmc-arceos.img`、`arm_64bit=1`、`enable_uart=1` 與 `dtoverlay=disable-bt`。先核對板卡 revision 與韌體保留記憶體，接 console 並確認輸出到 `BMC_DONE`。接著在同一板用 64 位元 Linux 執行靜態重播程式，核對事件序列。完成這兩項後再接 GPIO 治具驗證 I/O。[Raspberry Pi 啟動設定](https://www.raspberrypi.com/documentation/computers/config_txt.html) · [PL011 配置](https://www.raspberrypi.com/documentation/computers/configuration.html#configure-device-tree-overlays-for-uarts)

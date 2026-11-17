# 唯讀管理 API 與 PWM／tach 模型

Linux 與 ArceOS 使用同一份管理程式與 `axbmc-core` 模型。在 QEMU AArch64 `virt` 上以 HTTP 查詢 `GET /v1/status`，回應包含感測器溫度與狀態、電源使能與狀態，以及風扇的 RPM、PWM duty、週期、取樣電位和保護狀態。溫度欄位使用毫攝氏度。RPM 無有效量測時回傳 JSON `null`。

| 請求 | 回應 |
|---|---|
| `GET /v1/status` | `200`，三個元件的模擬狀態 |
| `GET` 未知路徑 | `404` |
| `POST` 等其他方法 | `405`，附 `Allow: GET` |

API 採本機 QEMU 網路驗證。腳本限制 QEMU 網路並將轉發綁在 `127.0.0.1`。服務使用 HTTP、一次處理一條連線，驗證範圍為本機模擬查詢。

## 風扇模型

PWM 以 25 kHz 的 40,000 ns 週期產生，50% duty 的高電位區間為每個週期前 20,000 ns。保護設定的 100% duty 在整個週期維持高電位。模型採線性 duty 與理想轉速的關係，50% duty 對應 3,000 RPM。tach 每轉兩脈波，因此正常上升緣間隔為 10 ms。轉速由兩次上升緣間隔與每轉脈波數換算。

啟動五秒後停止產生 tach，最後一個有效邊緣起算 100 ms 後，RPM 變為 `null`，並由共用保護邏輯將 duty 切到 100%。波形相位依客體單調時鐘計算。此模型用於重現電位、脈波與失聯情境，轉速及失聯參數為模擬設定。

## 重跑

使用 [既有 Rust 與 ArceOS 建置工具](qemu-replay.zh-TW.md) 和乾淨的 G1 checkout。Linux kernel 及 `linux-virt.apk` 模組須同版。本次使用 Alpine v3.23 AArch64 的 `6.18.60-0-virt`、`linux-virt-6.18.60-r0.apk` 與 `busybox-static-1.37.0-r30.apk`。建置腳本會在暫用 G1 checkout 修正一處 Rust 2024 的 FFI 宣告，讓啟用網路所需的分頁模組可編譯。

```sh
sh tools/management-api/run.sh /path/to/disposable-g1 \
  /path/to/vmlinuz-virt /path/to/linux-virt.apk 6.18.60-0-virt \
  /path/to/busybox-static.apk results/management-api
```

用 `QEMU_BIN` 指定 QEMU 執行檔。腳本建置兩個客體，驗證正常及 tach 失聯時的 JSON、拒絕寫入、未知路徑及 PWM／tach 的取樣相位。此次完整建置、客體 log、QEMU 命令與回應存於 `results/qemu-management-final/`。

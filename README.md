# AeroBMC

AeroBMC 是一個以 ArceOS 為優先後端、Linux 為基準的 BMC 模擬專案。以 Rust 實作管理核心，透過 D-Bus 與上游 OpenBMC 的 ObjectMapper、bmcweb 和 phosphor-state-manager 整合。開發與驗收均使用軟體模擬，主目標為 QEMU AArch64 `virt`，`raspi4b` 作為補充開機驗證。

- [套件相依性、架構選項與階段驗收](docs/openbmc-porting-plan.zh-TW.md)
- [已查閱建置檔的相依性快照](docs/upstream-build-evidence.json)
- [ArceOS 優先架構與 Linux 效能比較計畫](docs/arceos-performance-plan.zh-TW.md)
- [感測器、控制與管理 API 初評](docs/workload-evaluation.zh-TW.md)
- [AArch64 Linux／ArceOS 重播結果與重現步驟](docs/qemu-replay.zh-TW.md)
- [13 ms 週期感測器延遲與 ArceOS tick 頻率比較](docs/qemu-periodic.zh-TW.md)
- [模擬風扇與電源控制的故障注入及時序結果](docs/qemu-control.zh-TW.md)
- [QEMU 虛擬 GPIO、IRQ 與逾時驗證](docs/qemu-gpio-irq.zh-TW.md)
- [唯讀管理 API 與 PWM／tach 風扇模型](docs/qemu-management-api.zh-TW.md)
- [板級研究紀錄與 QEMU 參考平台](docs/board-integration.zh-TW.md)

目前優先方向是 ArceOS 管理核心。Linux 和 ArceOS 已在同一 QEMU AArch64 機型重播相同感測器輸入，事件一致。效能結果以對應的 QEMU 組態與模擬工作負載為準。

## 軟體模擬交付狀態

AeroBMC 的 QEMU 模擬版已完成可重現的基本整合驗收。上游 OpenBMC 元件與既有 BMC-G1 profile、協定識別字維持原名，便於核對交付證據及測試。 [驗收摘要](docs/release/acceptance.json) · [重跑說明](bmc-runtime/README.md)

BMC-G1 Runtime 的 Linux／ArceOS 發行驗收已涵蓋控制、HTTPS、appliance、診斷及 OpenBMC 基本整合。可用 [發行驗收結果](docs/release/acceptance.json) 核對結果，並從 [Runtime 說明](bmc-runtime/README.md) 重跑。原生 Romulus 整合另通過正常電源動作、六項資源與 34 項 BMC-G1 Redfish profile 斷言。

完整 Redfish 相容性與原生意外掉電恢復仍有兩個發行限制。帳號並行條件寫入可出現多筆成功，且回應缺少 `@odata.etag`。持續掉電時，Runtime 已關閉虛擬輸出，Redfish 系統狀態仍停留在 `On`，在觀察窗內也未啟動恢復。收斂範圍固定為可重現的 QEMU Runtime 與基本 OpenBMC 整合，兩個限制保留為明確的驗收失敗。詳見 [原生整合收尾結果](docs/release/limitations.json)。

效能以同一組 HTTPS 連線重用測試為基準。ArceOS 的 QEMU CPU 用量為 4.91 秒，Linux 為 6.91 秒。ArceOS 的 HTTPS p99 為 6.92 ms，Linux 為 7.93 ms。兩者的關閉 p99 皆約 62 ms，ArceOS 有一次超過 65 ms，Linux 為零次。這組結果支持以 ArceOS 作為優先的模擬 Runtime，Linux 保留為基準。原始結果見 [HTTPS 負載測量](docs/release/https-performance.json)。

## 執行原型

需要 Rust 工具鏈（建議目前 stable）：

```sh
cargo test --workspace
cargo check -p axbmc-core
rustup target add aarch64-unknown-none
cargo check -p axbmc-core --target aarch64-unknown-none
cargo run --release -p bmc-replay -- traces/sensor.csv > /tmp/bmc-events.csv
diff -u traces/sensor.expected.csv /tmp/bmc-events.csv
```

`axbmc-core` 不使用外部依賴、heap 或 std。感測器 ID 為呼叫者指定；高門檻使用 `>= high` 觸發、`<= clear` 清除。讀取失敗立即設為 Unavailable，過期檢查需呼叫 `tick`。時間為呼叫者提供的單調奈秒；時鐘倒退會拒絕操作。事件序號為每個 sensor 的狀態轉移序號。

事件佇列使用單一擁有者模型，不是跨 CPU／IRQ 的同步佇列；滿時丟棄新事件並累加 dropped，感測器狀態仍更新。序號與 dropped 在 u64 上限飽和。消費者若要在掉事件後恢復，需重新讀取狀態。

重播工具使用單一模擬溫度感測器，單位為毫攝氏度，設定 80°C／75°C、1 秒過期；這是測試設定，非板級散熱規格。CSV 的時間是邏輯時間；stderr 的 elapsed 包含事件收集，只是 smoke timing，沒有尾延遲、預熱或多次重複，不能用作 OS 效能結論。

此原型尚未實作固定 inventory 表、多 sensor registry、低門檻、持久化、控制或網路。ArceOS 與 Linux guest 共用的重播程式位於 `tools/qemu-replay`。

2026-11-03 驗證：macOS ARM64、Rust 1.99.1；3 項核心測試通過，host 與 `aarch64-unknown-none` check 通過，trace 輸出與 expected 完全一致，空輸入／錯誤 action／倒退時間均被拒絕。裸機 target check 只確認函式庫可編譯，不代表已有可開機映像。

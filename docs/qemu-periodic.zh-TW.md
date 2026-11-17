# AArch64 QEMU 週期感測器延遲

使用同一 AArch64 QEMU `virt` 機型，每 13 ms 在記憶體產生一次模擬感測器讀值，交替觸發及清除高溫門檻。兩端使用同一程式，在同一任務內更新感測器並取出事件。每次開機先暖機 16 筆，再記錄 256 筆。每組獨立開機三次，共 768 筆。每筆檢查事件序號、時間及狀態，開機重播的六筆 CSV 事件亦逐行核對。

| 組態 | 喚醒延遲 p99 | 事件延遲 p99 | 喚醒到事件取出 p99 | 事件延遲最大值 | 逾期 |
|---|---:|---:|---:|---:|---:|
| Linux（100 Hz 對照輪） | 4.326 ms | 4.338 ms | 32.000 µs | 7.467 ms | 0/768 |
| ArceOS（100 Hz） | 11.334 ms | 11.335 ms | 3.008 µs | 12.538 ms | 0/768 |
| Linux（1,000 Hz 對照輪） | 4.369 ms | 4.381 ms | 30.000 µs | 7.898 ms | 0/768 |
| ArceOS（1,000 Hz） | 1.380 ms | 1.382 ms | 4.992 µs | 5.869 ms | 0/768 |

Linux 的兩輪使用相同核心與映像。括號內頻率指配對的 ArceOS 組態。p99 採 nearest rank。喚醒延遲從預定取樣時間量到任務醒來，事件延遲從同一時間量到取出事件。每筆喚醒到事件取出的時間以兩個時間戳相減。事件在下一次預定取樣時間或之後完成即記為逾期。UART 輸出在量測區段之後。

ArceOS 啟用 round-robin 及 timer IRQ。此分支在 timer tick 檢查睡眠任務的到期時間。100 Hz 組態雖然醒來後處理很快，等待喚醒的 p99 仍推高了整體延遲。把 tick 從 100 調至 1,000 Hz 後，事件 p99 從 11.335 ms 降至 1.382 ms，比同輪 Linux 的 4.381 ms 低約 68%。這是 TCG、單 vCPU、無額外負載、合成讀值與同任務事件消費下的量測。提高 tick 會增加中斷頻率，CPU、功耗、實際裝置 I/O 與 G1 推論負載下的影響尚待量測。[ArceOS timer 初始化](https://github.com/tony126051/aerotensorg1/blob/39220889fae5184885778feb222afa30be18c986/arceos/modules/axruntime/src/lib.rs)

## 重現

先依 [AArch64 重播步驟](qemu-replay.zh-TW.md) 建置預設 100 Hz 並執行。測 1,000 Hz 時，在同一份暫用 checkout 將 `arceos/configs/defconfig.toml` 的 `ticks-per-sec` 改為 `1000`，以相同 `FEATURES=multitask,sched-rr` 重新建置。可指定新的 `OUT_CONFIG` 路徑，並確認生成的設定檔含 `ticks-per-sec = 1000`。沿用同一 Linux kernel 和 initramfs，以新輸出目錄執行 `compare.py`。每個目錄的 `environment.txt` 保存映像 SHA-256 與 QEMU 命令。

本機逐筆結果位於 `results/qemu-aarch64-periodic/periodic.csv` 及 `results/qemu-aarch64-periodic-1000hz/periodic.csv`。

# ArceOS 優先：BMC 管理核心與 Linux 比較計畫

目前專案範圍已固定為全軟體模擬。主要比較使用同一 QEMU AArch64 `virt` 組態，`raspi4b` 提供補充開機驗證。本文較早規劃的實體板階段保留為歷史研究，現行開發以虛擬裝置、故障注入、回歸測試及模擬器內效能比較完成驗收。

日期：2026-11-03。狀態：設計與實驗規劃，沒有實測效能數據。第一階段以軟體模型開始，不需要假設 G1 已有實體感測器。

原型進度：已建立 `crates/axbmc-core` 與 `tools/replay`，完成單一感測器、門檻遲滯、失效／過期與固定容量事件佇列，並提供可重播 trace 和預期事件。下文的模組切分是完整目標；inventory、多感測器及 ArceOS runner 仍待實作。host 檢查在 macOS 執行，不是 Linux 基準數據。

## 1. 優先決策

先評估把 OpenBMC 的 sensor、threshold、event 與控制狀態語意移植成 Rust `no_std` 管理核心，保留 Linux 對照。暫不以移植完整 bmcweb／D-Bus／systemd 作為起點。效益假設是減少 IPC、序列化與動態配置，以及縮短控制路徑；這些假設需要實驗，不是已確認的 OS 效能結論。

ArceOS 能否更快，應拆成啟動、常駐記憶體、CPU 成本、尾端延遲與 I/O 等問題。若總耗時主要在 I2C、裝置轉換、網路或 Flash，縮短服務路徑也可能只有小幅改善。用 `總耗時 = 裝置等待 + 排程等待 + 資料處理 + 傳輸` 記錄各段，避免僅量測函式呼叫後推論整體加速。

## 2. 已查閱的 G1 分支能力

以下來自 G1 repository，而非假設它與 ArceOS 最新版完全相同。Cargo 宣告只證明模組與選項存在，尚未編譯、啟動或驗證板級功能。

| 證據 | 能支持的判斷 | 尚不能支持的判斷 |
|---|---|---|
| workspace 含 axnet、axfs、axtask、axhal、axstd、axlibc 及 HTTP 範例 | 有可進一步整合的模組與範例入口 | 不代表完整 POSIX、OpenBMC ABI 或可用的安全管理服務 |
| axnet 用 smoltcp，包含 TCP／UDP／DNS、IPv4／IPv6，並啟用 alloc | 有網路堆疊起點 | 不代表無配置、HTTPS、TLS 憑證管理或認證已完成 |
| axtask 有 FIFO、RR、CFS 與 preempt 選項 | 可控制實驗的排程組態 | 不能宣稱 ArceOS 天生沒有排程干擾、沒有中斷或符合硬即時要求 |
| axhal 含 IRQ、SMP、paging 及 aarch64 QEMU 平台選項 | 可優先調查 aarch64 QEMU bring-up | 不代表已有 BMC GPIO／I2C／PWM／watchdog 驅動 |
| axhal 的 tls feature 轉接 axcpu/tls | 這裡是執行緒區域儲存的功能線索 | 不能作為 Transport Layer Security 的證據 |

來源與 blob SHA：

- [workspace Cargo.toml](https://github.com/tony126051/aerotensorg1/blob/main/arceos/Cargo.toml)：`b0e1a67655eaaaefdf25bb93437d3125a067e79d`
- [axnet Cargo.toml](https://github.com/tony126051/aerotensorg1/blob/main/arceos/modules/axnet/Cargo.toml)：`25fbd33a4f0541e4aa94171d7d3b1338cc0e08a2`
- [axtask Cargo.toml](https://github.com/tony126051/aerotensorg1/blob/main/arceos/modules/axtask/Cargo.toml)：`29f0c9fc693faef85c73200d63f712f821b69dfd`
- [axhal Cargo.toml](https://github.com/tony126051/aerotensorg1/blob/main/arceos/modules/axhal/Cargo.toml)：`332a85e1b3aa8482f6ac5f363d2c66744c3ebf94`

## 3. 原型切分

建議新增下列模組；目前僅為設計，尚未建立程式碼：

| 模組 | 責任 | 邊界 |
|---|---|---|
| axbmc-core | 固定 inventory、型別化讀值、有效性、時間戳、門檻與事件 | no_std；熱路徑預先配置；不直接依賴 axnet／檔案系統 |
| axbmc-platform | 單調時鐘、sensor read、控制 write、錯誤與回饋 | mock 與實體驅動分離；不猜測 MMIO 位址 |
| axbmc-runtime | 週期排程、限額工作佇列、逾時與事件消費 | 明確指定 task／IRQ 模式與工作預算 |
| axbmc-api（後續） | 對外查詢及控制、TLS、認證與授權 | 網路端不得阻塞控制工作；不宣稱完整 Redfish |

最小資料語意：sensor ID、inventory ID、單位、值、取樣時間、有效／失效狀態、門檻、遲滯、事件序號。失效與過期值不能當成零；跨門檻只在狀態改變時發事件。固定容量佇列滿時需有可觀測的 overflow 計數及策略。

第一個垂直原型只做「可重播輸入 → sensor 狀態 → 門檻事件 → 記憶體事件佇列」，另有讀取失敗與時鐘邊界測試。先排除實體控制、PID、網路、持久化及 Flash 更新，讓同一核心可在 Linux 程式與 ArceOS app 中執行。Rust no_std 測試通過也不能當作 ArceOS 開機成功。

## 4. 三組對照，分清效益來源

| 組別 | 工作負載與平台 | 可回答的問題 |
|---|---|---|
| L-min | Linux 單一 Rust 程式，執行同一管理核心與重播資料 | 與 ArceOS 比較相同邏輯時的系統執行成本 |
| A-core | ArceOS app，執行相同管理核心與重播資料 | 排程、啟動與整體記憶體的差異 |
| L-obmc | Linux／OpenBMC，使用對應 sensor／event 功能及相同輸入語意 | 原版服務堆疊的整體成本與功能差異 |

A-core 對 L-min 用於觀察 OS／runtime 差異，仍需記錄編譯器與連結方式。L-min 對 L-obmc 用於觀察服務分拆、D-Bus 與功能配置的成本。A-core 對 L-obmc 是整體方案比較，無法單獨證明 Linux kernel 是瓶頸。

三組必須在共同支援的同一硬體上測量，固定 CPU 數／頻率、記憶體、裝置、取樣週期、輸入、輸出及記錄方式。若只有 QEMU，先做功能與流程比較；TCG、host 排程或虛擬時鐘所得數字不得外推成實體 G1 加速比。不同平台量測只能各自記錄，不能算 OS 加速倍數。

Linux 記錄 kernel、scheduler、CPU affinity、IRQ affinity、背景服務、電源管理與 realtime 設定；ArceOS 記錄 task、preempt、SMP、IRQ、allocator 與 driver features。先比較一般組態，再增加調校過的 Linux 作敏感度分析，保留全部結果。

## 5. 量測定義

| 指標 | 起訖與報告方式 |
|---|---|
| 核心處理時間 | 讀值已在記憶體可用 → 門檻計算與事件入列完成；不含裝置等待 |
| 事件端到端延遲 | 後端發布可觀察事件 → 消費者收到；明列 polling 週期與時鐘來源 |
| 控制週期抖動 | 實際起始時間減預定起始時間；另報完成是否超過 deadline |
| 啟動 | 分別報 firmware entry／kernel entry → 管理就緒；HTTPS 就緒獨立量測，不以第一行 UART 代表可管理 |
| 記憶體 | Linux 程式 RSS/PSS 與全系統占用分開；ArceOS 報映像、保留記憶體、heap high-water、stack；整體比較用相同邊界 |
| CPU 與能耗 | 每秒處理量、CPU busy time／cycles per sample；功耗須有相同量測設備及邊界 |
| API（後續） | 同 payload、連線重用、TLS 與權限檢查下報 latency、吞吐及錯誤率 |
| 推論干擾（若同機） | AI 單獨與 AI＋管理工作兩組比較 TTFT、decode 尾延遲、deadline miss 與資源占用 |

每一延遲報 p50、p95、p99、p99.9、觀察到的最大值、樣本數與丟失數。最大觀察值不等於最差情況上界。計時使用單調時鐘，估計讀鐘成本；不同端點時鐘未同步時不相減。記錄溢位、超時、錯誤與丟棄樣本，不能只保留成功資料。

## 6. 工作負載與重複方式

以下為實驗輸入，並非 G1 板卡規格：

- 感測器數量 16／64／256，週期 1000／100／10 ms；不合理的實體匯流排速率只用於記憶體後端壓力測試。
- 正常平穩、門檻附近往返、短時間全部跨門檻、部分來源失效與佇列飽和。
- 空載、CPU 壓力、記憶體壓力；後續另加網路突發、儲存延遲與同機 AI 工作負載。
- 每種穩態組合預熱 30 秒、量測至少 60 秒並獨立重複 5 次；樣本不足以支撐 p99.9 時延長測試並標記不足。冷開機另做至少 30 次。
- 輸入使用固定 seed／trace hash，測試順序交錯執行，保存每次結果而非只留最佳值。

## 7. 是否值得繼續的判定

先做功能一致性檢查：相同 trace 得到相同狀態轉移及事件；逾時、溢位和失效狀態可觀測；控制功能之後加入時另訂硬體保護條件。功能較少的原型只能用共同功能子集比較。

以下是待實驗驗證的專案篩選門檻，不是預測值：相對 L-min，A-core 若能在 p99 事件延遲或整體記憶體至少改善 20%，且其他必要指標沒有超過 5% 的退步，才擴展較昂貴的網路／控制移植。依各次重複結果評估變異與不確定性；差異落在噪聲範圍就延長測試，不宣布勝出。硬體需求確定後，絕對 deadline 與資源預算優先於百分比。

若只優於 L-obmc、未優於 L-min，結論是服務精簡有價值，不能歸因於 ArceOS 本身。若 I/O 主導且沒有可用效益，可保留 Linux 後端及已完成的共用管理核心。

## 8. 下一階段交付

1. 實作無外部依賴的 no_std 管理核心、mock 後端與正確性測試。
2. 建立共用重播 trace、Linux runner 與結果格式；host 環境結果只驗證工具，不當成 Linux／ArceOS 對照。
3. 固定 G1／ArceOS commit、調查建置工具與 aarch64 QEMU 啟動，建立 ArceOS runner。
4. 在共同硬體取得 L-min／A-core 數據後，加入最小 OpenBMC 對照與選定實體裝置。

結果紀錄至少包含 run ID、source commits、target、kernel/features、compiler flags、硬體／QEMU 版本、clock、workload、seed、warmup、duration、samples、errors、drops 及原始量測檔。此文件不填入任何未實測的加速倍數。

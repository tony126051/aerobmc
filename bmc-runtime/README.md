# AeroBMC Runtime

獨立 Rust 專案。第一個可執行版本提供常駐控制迴路、模擬事件輸入及本機 HTTP。控制迴路持有可變狀態，HTTP 讀取已發布的快照。逾時判定由控制迴路執行。

```sh
cargo test --manifest-path bmc-runtime/Cargo.toml
cargo run --manifest-path bmc-runtime/Cargo.toml -- 5555
```

服務監聽 `127.0.0.1`。連接埠可由命令列指定，傳入 `0` 會由系統選擇可用連接埠並在啟動時印出。另一個終端執行：

```sh
curl http://127.0.0.1:5555/v1/status
curl http://127.0.0.1:5555/v1/events
```

管理程式可透過無內容的 `POST /v1/actions/{action}` 排入 `start`、`stop`、`cycle` 和 `fan-reset`。請求必須帶 `X-BMC-Action: 1`。收到 `202 Accepted` 表示已排入共用佇列，實際結果由控制迴路依目前狀態和保護條件決定，可讀取狀態和事件確認。佇列滿時回傳 `503`。HTTP 不提供感測器或 GPIO 回饋注入端點。這個標頭用來要求呼叫端明確提出操作。此介面供本機模擬使用，沒有身分驗證。QEMU 的主機轉發限於 loopback。

```sh
curl -X POST -H 'X-BMC-Action: 1' http://127.0.0.1:5555/v1/actions/start
```

模擬輸入從程序的標準輸入逐行送入。指令包括 `temperature 42000`、`tach 3000`、`start` 和 `power-good`。溫度單位為毫攝氏度，tach 輸入單位為 RPM。每次電源啟動與轉速失聯使用 100 ms 的模擬期限。

轉速失聯時，風扇保持 100% 保護輸出，直到轉速回饋穩定且收到 `fan-reset`。復歸要求控制迴路連續收到至少 2,000 RPM 的樣本，首末合格樣本間隔至少 100 ms，且各次回饋與最後一次回饋至復歸指令的間隔都小於 100 ms。低於門檻的樣本或回饋逾時會重算穩定區間。條件不足時記錄 `fan_reset_rejected`。成功時記錄 `fan_recovered`，由當前溫度決定 50% 或 75% 輸出。QEMU 客體使用 UART 接收復歸指令，轉速取自 GPIO4。上述數值是模擬設定。

電源每輪最多嘗試三次。前兩次逾時後，輸出拉低並在 `retry_wait` 等至少 100 ms 再啟用。第三次逾時進入 `failed` 並保持輸出關閉，新的 `start` 才會重置嘗試次數。每次啟用需要本次嘗試的 power-good 邊緣，等待重試時或上次嘗試留下的回饋會被忽略。GPIO 邊緣與控制期限使用客體單調時鐘，狀態機以毫秒判定。API 提供 `power_attempt` 與 `power_attempt_limit`，事件包含 `power_retry_scheduled`、`power_retry_started` 及 `power_locked`。

從標準輸入送出 `stop`，QEMU 客體則從 UART 送出，可將 `starting`、`retry_wait` 或 `on` 切到 `off`。控制迴路拉低電源使能，取消重試，將嘗試次數歸零，停止主機 watchdog，並記錄一筆 `power_stopped`。重複送出 `stop` 會保留目前的關閉或故障狀態。若 `start` 緊接在 `stop` 後排入，控制迴路會先發布關閉輸出，再於下一輪處理啟動。這是模擬 BMC 直接切換電源使能的操作。狀態和事件端點維持唯讀。

當電源為 `on` 時，可從同一輸入介面送出 `cycle`。控制迴路進入 `cycle_wait`，關閉主機 watchdog 並拉低電源使能。從發布低電位起等待至少 100 ms 後，自動開始一輪新的啟動嘗試。等待期間的 `start`、`cycle` 與 power-good 上升緣都無法提前結束等待。新的啟動沿用 power-good 確認與重試規則。這段流程記錄 `power_cycle_scheduled` 和 `power_cycle_started`。等待時可用 `stop` 取消上電。100 ms 是模擬設定。

電源進入 `on` 時啟動 500 ms 主機 watchdog。GPIO6 心跳上升緣的時間戳須新於上次接受的時間，才能延長期限。控制迴路先判定逾時，期限到達時拉低電源輸出、發布 `watchdog_tripped` 事件並等待新的 `start`。API 的 `watchdog` 欄位包含啟用狀態、觀察到的心跳邊緣數、觸發次數及期限。觀察到的邊緣數也包含逾時後收到的訊號，方便核對失效時的裝置行為。

QEMU 客體在 `on` 期間也會從 GPIO5 取樣 power-good。從首次讀到低電位起，若低電位樣本持續至少 50 ms，BMC 就關閉電源輸出、停止主機 watchdog，並將狀態與事件記為 `power_good_lost`。期間若取樣讀到高電位，就重新計算下一次低電位的持續時間。電源關閉後，即使 GPIO5 恢復高電位，仍須明確送出 `start` 並提供本次嘗試的新上升緣。50 ms 是模擬門檻，以客體控制迴路取樣時間計算。

`temperature <id> <毫攝氏度>` 可指定感測器。原有 `temperature <毫攝氏度>` 表示感測器 0。`/v1/status` 的 `sensors` 陣列回傳各感測器的讀值和狀態，既有 `sensor` 欄位仍對應感測器 0。

| ID | 感測位置 | 高溫門檻 | 解除門檻 |
|---|---|---:|---:|
| 0 | 進氣 inlet | 80°C | 75°C |
| 1 | 板區 board | 65°C | 60°C |

任何一筆已取樣的溫度達高溫門檻，或 1 秒沒有更新，風扇即由 50% 提高到 75%。過期讀值回傳 `null`。高溫要降至解除門檻才解除，且過期後恢復的讀值沿用原本的遲滯狀態。所有已監控的感測器恢復正常後回到 50%。tach 失聯保護的 100% 優先。這些數值是模擬設定。

首次收到有效溫度後，若已無任何有效溫度讀值，BMC 會取消啟動、重試或斷電重啟，或關閉運行中的主機電源。API 和事件會記為 `thermal_telemetry_lost`。此時送出 `start` 會記錄 `thermal_start_blocked`。至少一個感測器恢復有效讀值且沒有危急標記時，才可用新的 `start` 上電。尚未收到過任何讀值的模擬開機仍可使用電源指令。

| 感測位置 | 危急門檻 | 危急解除門檻 |
|---|---:|---:|
| 進氣 inlet | 95°C | 85°C |
| 板區 board | 85°C | 75°C |

任一感測器進入危急狀態，風扇輸出升至 100%。若正在供電、啟動或等待重試或斷電重啟，控制迴路會切到 `thermal_tripped`、關閉電源輸出並停止 watchdog。API 的 `sensors[].critical` 會保留危急標記，即使讀值已過期。每個已觸發的感測器都必須收到達解除門檻或更低的新讀值，BMC 才接受 `start`。降溫後仍需明確啟動。事件會記錄危急狀態、觸發斷電的感測器及被阻止的啟動。門檻是模擬設定。

`GET /v1/events` 讀取常駐控制迴路的事件，依序回傳 `sequence`、控制迴路處理時的 `at_ms`、可與狀態快照對照的 `revision` 及 `kind`。目前記錄 `power_started`、`power_timed_out`、`power_good_ignored`、`power_on` 與 `fan_protection`。風扇進入保護時記一次。事件保留最近 32 筆於記憶體，覆蓋舊紀錄時 `overwritten` 會增加，重開機後重新計數。

感測器另記錄 `sensor_high`、`sensor_cleared`、`sensor_stale` 和 `sensor_recovered`，以 `sensor_id` 指出來源。

## AArch64 QEMU

同一份 Runtime 原始碼可建置為 Linux initramfs 服務及 ArceOS 客體。QEMU 版透過 UART 接收啟動與溫度輸入，透過 PL061 GPIO 中斷接收 tach 與 power-good。HTTP 經 QEMU loopback 轉發。控制迴路自行處理 tach 失聯及電源逾時，並在更新 GPIO 輸出後發布快照。

準備 Rust nightly-2025-05-20、`aarch64-unknown-linux-musl` 和 `aarch64-unknown-none-softfloat` 目標、ArceOS 建置工具、QEMU、Python 3、cpio，以及可供腳本暫時修改的 AeroTensor-G1 checkout。這次平台 commit 為 `39220889fae5184885778feb222afa30be18c986`。Linux 使用 Alpine `6.18.60-0-virt` kernel，搭配同版的 `linux-virt-6.18.60-r0.apk` 與 AArch64 BusyBox static APK。

```sh
sh bmc-runtime/qemu/run.sh /path/to/disposable-g1 \
  /path/to/vmlinuz-virt /path/to/linux-virt.apk 6.18.60-0-virt \
  /path/to/busybox-static.apk results/runtime-qemu
```

可用 `QEMU_BIN` 指定執行檔。腳本建置兩個客體並保存映像、建置記錄、QEMU 指令、序列埠 log 和 API 快照。QEMU 必須支援 qtest。ArceOS 客體停用預設 UART 回顯中斷，讓模擬輸入執行緒負責接收字元，並在核心位址空間映射 PL061 的 MMIO 頁。

| PL061 腳位 | 方向 | 模擬用途 |
|---|---|---|
| GPIO0 | 輸出 | 電源使能 |
| GPIO1 | 輸出 | 高電位選擇 100% 保護 PWM |
| GPIO2 | 輸出 | GPIO1 為低且 GPIO2 為高時選擇 75% PWM，兩者都低時選擇 50% |
| GPIO4 | 輸入 | 風扇 tach 上升緣，每轉兩脈波 |
| GPIO5 | 輸入 | power-good 上升緣與當前電位 |
| GPIO6 | 輸入 | 主機 watchdog 心跳上升緣 |

主機治具透過 qtest 讀回 GPIO0／GPIO1／GPIO2，再依 duty 計算 25 kHz 的理想 PWM 波形。50% duty 對應理想轉速 3,000 RPM，治具按對應間隔發出 GPIO4 脈波。客體用兩個實際中斷的時間戳換算 RPM。QEMU 排程會影響脈波間隔，50% duty 測試容許 2,000–4,500 RPM。25 kHz 波形在主機軟體中按相位取樣。[QEMU qtest 介面](https://github.com/qemu/qemu/blob/v10.2.2/system/qtest.c)

驗證涵蓋正常轉速、停送 tach 後的失聯保護、未收到 power-good 的電源逾時、遲到回饋及重新啟動。主機同時比對 GPIO 暫存器和 API 快照。API 的 `power_good_edges` 計數可確認客體控制迴路收到回饋，即使狀態機因為逾時而保留 `Failed`。QEMU 版只接受來自 GPIO 的 tach 與 power-good，避免透過 UART 繞過裝置路徑。

重跑腳本也會輸出 `linux-events.json` 與 `arceos-events.json`，並檢查事件順序、狀態版本與 GPIO 驗證相符，以及 HTTP 寫入事件的請求回傳 `405`。

溫控整合驗收核對兩個感測器的高溫、遲滯、過期和恢復。升溫或過期時 API 與 GPIO 同步顯示 75%，正常後回到 50%。Linux、ArceOS 各核對 14 筆事件，包含既有電源與風扇保護流程。結果位於 `results/runtime-thermal/`。

重試整合各在 Linux 與 ArceOS 跑兩次開機。一組驗證三次逾時、等待期間輸出拉低、舊高電位不會使重試成功、鎖定後仍忽略遲到訊號，以及新指令可重新啟動。另一組在第二次嘗試給出有效回饋，驗證不再進行第三次。建置腳本在暫用 ArceOS checkout 為關閉 TCP 前的待送資料提供最多 200 ms 的送達時間，以便保留完整的事件回應。

Watchdog 整合延續第二次啟動成功情境。主機注入四次 GPIO6 脈波，再停止心跳並暫停 HTTP 查詢。測試比對輸出關閉、事件與 API，確認遲到脈波和等待期間都不會重新啟用電源，接著以新指令和 power-good 完成重啟。結果位於 `results/runtime-watchdog/`。這個 watchdog 由 BMC 控制迴路監控模擬主機心跳。

電源回饋失效整合接續 watchdog 後的重啟。Linux 和 ArceOS 各驗證約 15 ms 的低電位脈波、心跳仍有效時的持續低電位、GPIO0 關閉、單筆故障事件、回饋恢復後維持關閉，以及以新啟動指令和上升緣重新供電。各客體的 `*-power-good-loss.json` 位於 `results/runtime-power-good-loss/`。

主動斷電整合驗證運行中連送兩次 `stop` 只記錄一次事件。關閉後送入遲到的 power-good 與心跳，確認維持關閉且 watchdog 未觸發。接著在重試等待期間送出 `stop`，跨過兩個重試間隔後比對事件與 GPIO0，再以 `start` 和新的 power-good 上升緣確認可重新供電。兩個客體的 `*-power-stop.json` 位於 `results/runtime-power-stop/`。

斷電重啟整合檢查 `cycle_wait` 的 GPIO0 低電位、啟動事件間隔、舊回饋不會確認本次啟動，以及新回饋後重新啟動 watchdog。第二輪在等待期間以 `stop` 取消，確認 GPIO0 保持低電位。兩個客體的 `*-power-cycle.json` 位於 `results/runtime-power-cycle/`。

風扇復歸整合檢查失聯時拒絕復歸、回饋穩定後維持保護直到收到指令、高溫時回到 75%、降溫後回到 50%，以及再次失聯時回到 100%。驗證比對 API、GPIO 輸出和事件。兩個客體的 `*-fan-recovery.json` 位於 `results/runtime-fan-recovery/`。

危急過溫整合在運行時注入兩個感測器的危急讀值，驗證 GPIO0 關閉、風扇輸出 100%、watchdog 停止，並在兩筆讀值過期後檢查危急標記。只解除其中一個感測器時，啟動仍被拒絕。兩個感測器都降至解除門檻後，以 `start` 和新的 power-good 上升緣重新上電。兩個客體的 `*-thermal-trip.json` 位於 `results/runtime-thermal-trip/`。

溫度回饋中斷整合在持續送出心跳時讓兩個感測器依序過期。測試驗證單一感測器過期時維持供電、最後一筆讀值過期時關閉 GPIO0，以及回饋恢復後必須明確啟動並收到新的 power-good。兩個客體的 `*-thermal-telemetry.json` 位於 `results/runtime-thermal-telemetry/`。

管理操作整合在兩個客體透過 HTTP 執行啟動、停止、斷電重啟與風扇復歸，並核對 GPIO、狀態和事件。結果位於 `results/runtime-actions/` 的 `*-actions.json`。

## Runtime 效能比較

先用上面的 `run.sh` 建置映像並跑功能回歸，再用同一組映像執行：

```sh
python3 bmc-runtime/qemu/benchmark.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-actions results/runtime-performance
```

量測以 Linux、ArceOS、ArceOS、Linux 的順序分別開機。每次開機先讓風扇進入失聯保護，再做 50 次電源故障注入。背景以 25 Hz 提出狀態查詢，每 350 ms 啟動一次主機，確認 power-good 後拉低 GPIO5，並核對 GPIO0 關閉、watchdog 解除及 `power_good_lost` 事件。延遲從主機開始送出 GPIO5 低電位命令，到約每 1 ms 輪詢時首次讀到 GPIO0 關閉，包含 50 ms 模擬低電位門檻、QEMU 與主機觀測成本。65 ms 為本次比較使用的逾期門檻。CPU 秒數取自 QEMU 行程的累計 CPU 時間，包含模擬裝置及查詢，排除開機與風扇準備階段。

`manifest.json` 記錄設定與映像雜湊，各次開機保存 QEMU 指令、序列埠輸出與原始量測。`summary.json` 彙整 nearest-rank p50／p99、逾期、HTTP 錯誤與 QEMU CPU 秒數。

本輪各平台 100 次注入皆確認 GPIO 關閉及預期事件。Linux／ArceOS 的 p99 分別為 73.57／68.81 ms，超過 65 ms 的比例為 3%／12%，QEMU CPU 時間為 6.75／35.06 秒。ArceOS 在本輪的 p99 低 6.5%，但超過 65 ms 的次數更多，QEMU CPU 時間約為 Linux 的 5.2 倍。兩邊分別有 8／2 次背景 HTTP 查詢錯誤，共各 876 次。首次試跑曾遇到主機 TCP 連線錯誤，後續使用 25 Hz 查詢並以 GPIO 直接同步 power-good。這組數值適用於此 TCG 工作負載。

### ArceOS 空閒等待改善

固定平台版本的 UART `Stdin` 和 smoltcp TCP 等待會反覆 yield，使工作保持可排程。Runtime 在 ArceOS 改用非阻塞 UART 讀取，無資料時休眠 1 ms。建置腳本在暫用平台 checkout 中，讓 TCP 等待與關閉前的送達等待也休眠 1 ms，並把 timer tick 從 100 Hz 調為 1,000 Hz。Linux 沿用原有的阻塞 I/O。

依原流程重建到 `results/runtime-idle`，再用 `benchmark.py` 將結果寫入 `results/runtime-performance-idle`。13 項單元測試與雙平台 QEMU 功能回歸均通過。重新量測的 Linux／ArceOS 各 100 次故障均關閉 GPIO 並留下預期事件，超過 65 ms 的次數均為零。p99 分別為 62.11／61.21 ms，QEMU CPU 時間分別為 6.64／2.76 秒，背景 HTTP 錯誤為 3／1 次（各 876 次）。這是三項調整合併後的 TCG 結果，各項調整的個別貢獻尚未量測。

### 管理查詢負載比較

以同一組 `results/runtime-idle` 映像，依序用 `benchmark.py --http-hz 0`、`25`、`50`、`100` 執行，每個頻率指定獨立輸出目錄。0 表示停用背景查詢，保留故障試次必要的 HTTP 操作。每個頻率、每個客體有兩次開機及 100 次故障注入。查詢由單一背景工作依預定時間送出。原始結果記錄成功回應時間、發送落後時間與錯誤。50 Hz 的第一輪遇到 ArceOS 事件查詢連線重設而中斷，保留在 `50hz-interrupted`。正式重跑允許唯讀事件查詢最多三次，啟動操作和背景查詢各只送一次。

| 背景查詢／秒 | Linux／ArceOS 斷電 p99（ms） | Linux／ArceOS 超過 65 ms | Linux／ArceOS QEMU CPU（秒） | Linux／ArceOS 背景 HTTP 錯誤 |
|---:|---:|---:|---:|---:|
| 0 | 62.11 / 60.60 | 0 / 0 | 3.80 / 2.23 | 無背景查詢 |
| 25 | 61.82 / 60.45 | 0 / 0 | 6.11 / 2.62 | 2 / 3（各 876 次） |
| 50 | 62.10 / 60.86 | 0 / 0 | 8.53 / 2.99 | 5 / 7（各 1,750 次） |
| 100 | 60.97 / 62.31 | 0 / 1 | 13.04 / 3.77 | 9 / 7（各 3,500 次） |

各頻率都驗證 GPIO 關閉及預期事件，正式結果位於 `results/runtime-performance-sweep`。100 Hz 是最高測試負載，效能數值適用於此次 QEMU TCG 設定。

### HTTP 連線重用

HTTP/1.1 的狀態與事件 GET 可用 `Connection: keep-alive` 重用連線。每條連線最多 256 個請求，逐次讀完回應再送下一個請求。Runtime 同時最多處理 8 條連線，等待無資料時約 1 秒後結束。動作 POST 完成回應即關閉連線。Linux 使用 socket timeout，ArceOS 的建置腳本在暫用平台 checkout 為 TCP 等待加上期限。

以 `results/runtime-keepalive` 的同一組映像，用 `benchmark.py --http-hz 100` 比較是否加上 `--keep-alive`。故障流程與背景查詢共用主機端的 HTTP 發送鎖，GPIO 量測仍與背景查詢並行。兩個模式、兩個平台各驗證 100 次斷電及事件。結果位於 `results/runtime-keepalive-close` 與 `results/runtime-keepalive-reuse`。先前試跑在兩種模式各發生一次 HTTP 狀態確認超時而中斷。

| 指標 | Linux 逐次建連 → 重用 | ArceOS 逐次建連 → 重用 |
|---|---:|---:|
| QEMU CPU 時間（秒） | 14.65 → 6.89 | 6.72 → 3.10 |
| 斷電 p99（ms） | 61.59 → 61.63 | 62.81 → 62.71 |
| 超過 65 ms | 0 → 0 | 0 → 0 |
| 背景 HTTP 錯誤（各 3,500 次） | 0 → 0 | 0 → 1 |
| 背景查詢發送落後 p99（ms） | 32.88 → 35.99 | 31.14 → 53.72 |

兩個平台重用模式各有 3,486 次背景請求使用既有連線。本輪觀察到 QEMU CPU 成本下降，HTTP 錯誤率改善未獲驗證。

### 管理指令結果

`POST /v1/actions/{action}` 的 `202 Accepted` 回應包含 `queued: true` 和數字 `id`。用 `GET /v1/actions/{id}` 查詢該指令。排隊時回傳 `pending`，控制迴路處理並發布 GPIO 與狀態後回傳：

| 狀態 | 意義 |
|---|---|
| `applied` | 指令已套用。例如 `start` 已開始啟動，後續是否上電仍由 power-good 與逾時決定。 |
| `rejected` | 保護條件拒絕指令，`event` 會指出拒絕原因。 |
| `no_op` | 當前狀態無需執行指令。例如電源已關閉時再送 `stop`。 |

已處理結果帶有 `revision` 和 `event`。無對應事件時 `event` 為 `null`。Runtime 在記憶體保留最近 32 筆已處理的 HTTP 指令。查不到 ID 時回傳 `404`，重啟後 ID 重新從 1 起算。UART 和 HTTP 共用指令佇列，結果查詢適用於 HTTP 提交的指令。

雙平台驗證結果位於 `results/runtime-receipts` 的 `*-action-receipts.json`。

### 管理指令重送

操作請求可帶選用的 `Idempotency-Key` 標頭。鍵值區分大小寫，長度 1–64 字元，可使用 ASCII 英數、連字號、底線和句點。同一個鍵重送同一個操作時，BMC 回傳原本的 `202` 和指令 ID，即使佇列已滿也可找回該 ID。若以相同的鍵提交另一種操作，回傳 `409 Conflict`。仍需帶 `X-BMC-Action: 1`。

重送辨識涵蓋排隊中的指令及最近 32 筆已處理的 HTTP 指令。原結果淘汰或 Runtime 重啟後，再用同一個鍵會建立新指令。兩平台的整合測試在上電恢復後重送原本的 `cycle`，並核對事件序列和相同鍵值的衝突回應。結果位於 `results/runtime-action-replay` 的 `*-action-replay.json`。

## Redfish 管理介面

從 `GET /redfish/v1/` 探索 Systems、Chassis、Sensors 與 TaskService。模擬主機顯示 `PowerState`，兩個溫度感測器回傳攝氏讀值和可用狀態。Reset 動作接受 `On`、`ForceOff`、`PowerCycle`，共用原有控制迴路、保護條件及指令去重。操作仍須帶本機介面的 `X-BMC-Action: 1`。

```sh
curl -i -H 'X-BMC-Action: 1' -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-power-on' -d '{"ResetType":"On"}' \
  http://127.0.0.1:5555/redfish/v1/Systems/Host/Actions/ComputerSystem.Reset
```

`202` 回應的 `Location` 可查詢任務監控，回應內的 `@odata.id` 可讀取任務資源。任務完成表示控制迴路已處理指令。啟動或重啟後的實際上電狀態可從 ComputerSystem 資源確認。任務沿用記憶體內 32 筆已處理指令的保留窗口。

此介面實作上述 Redfish 資源及動作子集，驗收使用本機 HTTP 和 AArch64 QEMU。TLS、Redfish 工作階段認證及完整相容性驗證仍待開發。兩平台測試結果位於 `results/runtime-redfish` 的 `*-redfish.json`。[DMTF Redfish 規格](https://redfish.dmtf.org/schemas/DSP0266_1.16.0.html)

### Redfish 故障診斷

ComputerSystem 的 `Status.Health` 彙整電源故障、溫度和風扇保護狀態。`Oem.BmcG1.PowerFault` 提供目前的電源故障原因，`Revision` 可與 Runtime 事件對照。從 ComputerSystem 的 `LogServices` 連結可探索 Runtime LogService 與 Entries。事件包含序號、嚴重度、單調時間與狀態版本。感測器相關事件提供 `Links.OriginOfCondition`，可直接讀取來源感測器。

LogService 與原有事件 API 共用最近 32 筆記憶體紀錄，並顯示覆寫筆數。已覆寫的條目回傳 `404`。單調時間以 Runtime 啟動為基準。重啟會清空紀錄。

雙平台整合測試注入板區過溫，從主機狀態與日誌找到故障來源，確認過熱期間的 Redfish `On` 任務被拒，再用降溫讀值與新 power-good 驗證復歸。結果位於 `results/runtime-diagnostics` 的 `*-redfish-diagnostics.json`。

## 持續監控與事件歸檔

主機端收集器用 Python 3 標準庫輪詢與 Redfish LogService 共用的 `/v1/events`。事件、進度和告警在同一 SQLite 交易寫入。可使用 `--once` 單次收集，或以預設 250 ms 間隔持續輪詢。連線失敗時保留原進度，下次成功讀取會接續歸檔。使用固定的 `--source` 可在同一 BMC 的轉發連接埠變更後繼續使用既有進度。

```sh
python3 bmc-runtime/qemu/monitor.py http://127.0.0.1:5555 bmc-events.sqlite --source g1
python3 bmc-runtime/qemu/verify_monitor.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-diagnostics results/runtime-monitor
```

資料庫的 `events` 保存原始事件，`alerts` 記錄 `events_missing` 和 `stream_reset`。日誌覆寫後，缺失範圍以序號表示。當最新序號倒退，或仍保留的上一筆事件內容改變，收集器會建立新的事件世代。若重啟發生在兩次輪詢之間，且新序號已超過舊進度並覆寫對照事件，收集器無法區分重啟與一般覆寫。兩個客體的測試報告與 SQLite 歸檔位於 `results/runtime-monitor/`。

## 管理介面存取控制

每次開機可透過受信任的 UART（主機版為標準輸入）送出 `auth-enable <reader-token> <operator-token>`，收到 `BMC_AUTH_ENABLED` 後所有管理 HTTP 端點都會要求 `Authorization: Bearer <token>`。兩個 token 必須不同，各使用 32–64 個 ASCII 英數字元。每次開機只接受一次設定，憑證保存在記憶體，重啟後須重新設定。預設開機保留本機模擬模式，請在開放管理端存取前完成設定。

- Reader 可讀取 Runtime 與 Redfish 的狀態、日誌和任務結果。
- Operator 另可提交操作，仍需帶 `X-BMC-Action: 1`。
- 缺少或無效的憑證回傳 `401`，Reader 嘗試提交操作回傳 `403`。
- 事件收集器可用 `monitor.py --token-file <path>` 讀取 Reader 憑證。

此模式使用本機 HTTP，QEMU 轉發限於主機 loopback。Bearer 憑證會經由 HTTP 傳輸。TLS 與 Redfish SessionService 尚待開發。驗證結果位於 `results/runtime-access/`。

## HTTPS 管理閘道

先用 UART 啟用 Reader／Operator，再將 QEMU 的 HTTP 後端轉發限制在主機 loopback。提供與管理端使用的主機名稱或 IP 相符的憑證與私鑰，再啟動閘道：

```sh
python3 bmc-runtime/qemu/gateway.py --backend-port 5555 --port 8443 \
  --cert /path/to/server.pem --key /path/to/server-key.pem
```

客戶端需信任憑證的簽發者，並照原 API 傳送 Bearer token。閘道預設只監聽 loopback，使用 TLS 1.2 以上，將請求轉送給本機 Runtime；角色與操作保護仍由 Runtime 執行。若後端尚未啟用認證，閘道會拒絕啟動。每筆轉送也要求 Runtime 驗證 Bearer token，以涵蓋客體重啟後尚未設定 token 的情況。此架構在主機終止 TLS，閘道到客體使用 loopback 轉發的 HTTP。

```sh
python3 bmc-runtime/qemu/verify_gateway.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-https-images results/runtime-https
```

驗證使用暫時測試憑證，檢查信任鏈、名稱驗證、角色、Redfish 任務和 GPIO。

### HTTPS 歸檔與重啟復原

每次客體開機時，透過可信任 UART 傳入 `auth-enable <reader> <operator> <stream-id>`。第三個值是主機佈建端為該次開機產生的 32 位十六進位識別碼，需每次更新，例如使用 `openssl rand -hex 16`。識別碼由已認證的事件端點回傳。即使新客體的事件序號已超過舊進度並覆寫比對紀錄，收集器仍能建立新世代。舊版 HTTP 模式可沿用序號判斷。

```sh
python3 bmc-runtime/qemu/monitor.py https://127.0.0.1:8443 bmc-events.sqlite \
  --source g1 --ca-file /path/to/ca.pem --token-file /path/to/reader-token
```

收集器每次輪詢重新讀取 token 檔，方便在重新佈建後換用新憑證。保持同一 `--source` 與資料庫即可保存舊歷史。客體重啟期間，閘道要求每次轉送都通過後端認證。驗收在同一閘道跨越兩次客體開機，結果位於 `results/runtime-secure-recovery/`。

## 一鍵啟動模擬 BMC

先依前述方式建置 Linux 與 ArceOS 映像，再用同一入口啟動任一版本。輸出目錄必須是新目錄，啟動器會建立權限為 0700 的目錄，放入憑證 token、序列埠紀錄與事件資料庫。

```sh
python3 bmc-runtime/qemu/appliance.py arceos /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-secure-recovery-images /path/to/new-run \
  --cert /path/to/server.pem --key /path/to/server-key.pem --ca-file /path/to/ca.pem
```

`ready.json` 出現後，可從其中取得 HTTPS 連接埠。服務預設在 `127.0.0.1:8443`，可透過 `--port` 指定其他連接埠。客體終止後，啟動器會在同一 HTTPS 入口下重新開機、產生並佈建新 token 和事件識別碼，持續使用原 SQLite 資料庫。管理端可從 `reader.token` 或 `operator.token` 重新讀取 token。預設最多重啟三次，可用 `--restart-limit` 調整。按 Ctrl+C 停止整組服務。啟動器每隔約 1 秒向客體送出帶 Reader token 的事件查詢，並核對本次開機識別碼。連續三次失敗會重啟客體。單次失敗後若恢復，計數歸零。`ready.json` 的 `restart_reason` 記錄前一輪退出原因。

```sh
python3 bmc-runtime/qemu/verify_appliance.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-secure-recovery-images results/runtime-appliance
```

驗收透過 HTTPS 發出 Redfish 啟動指令，故意不提供 power-good，並核對持續收集器歸檔電源鎖定事件。接著強制終止 QEMU，驗證自動重開、原 HTTPS 連接埠、舊 token 拒絕與新世代事件。兩個版本的結果皆保留在 `results/runtime-appliance/`。

健康檢查驗收另以 SIGSTOP 暫停 QEMU，確認行程仍存在時也會自動復原，並驗證同一 HTTPS 連接埠與資料庫連續保存三個開機世代。結果位於 `results/runtime-appliance-health/`。健康檢查涵蓋管理 HTTP 和開機識別碼。已認證的事件回應也提供 `control_progress`。每輪控制迴路完成 GPIO 更新後，才發布新的進度；即使 HTTP 回傳 200，連續三次進度未增加仍會以 `control_stalled` 為原因重啟。

控制迴路停滯驗收需在執行 `run.sh` 時設定 `BMC_TEST_CONTROL_STALL=1`，再將所得映像交給 `verify_appliance.py`。這個建置設定讓測試能以 UART 停住控制迴路並保留 HTTP 回應。一般建置無法透過此指令注入停滯。結果位於 `results/runtime-control-health/`。

### 復原紀錄與重啟額度

`lifecycle.jsonl` 依序記錄開機嘗試、服務就緒、故障原因與重啟額度耗盡，每筆包含開機世代和主機時間。額度耗盡時，啟動器會移除 `ready.json`、關閉 HTTPS 與收集器、保留 SQLite 和客體紀錄，並以退出碼 1 結束。`failure.json` 保留最後一輪的故障原因。預設三次重啟代表最多四次開機嘗試。

使用控制停滯測試映像執行 `verify_appliance.py`。驗收依序注入客體退出、客體暫停、控制迴路停滯與最後一次客體退出，確認沒有第五次開機、HTTPS 已關閉，且仍可讀取四個世代的故障紀錄。結果位於 `results/runtime-restart-limit/`。

### 重啟額度耗盡後復役

確認故障原因後，使用原啟動指令、同一個輸出目錄與同一客體類型，加上 `--resume`。啟動器會確認目錄有重啟額度耗盡的紀錄，並拒絕同時操作同一目錄的第二個程序。原 `failure.json` 會移到 `failure-<世代>.json`，開機紀錄接續編號，SQLite 事件歷史保留。復役會重新計算重啟額度，並產生新的 Reader／Operator token。若要沿用固定 HTTPS 連接埠，復役時請指定同一個 `--port`。

`--resume` 適用於啟動器已完整寫出額度耗盡紀錄的情境。若在復役過程中強制終止啟動器，需人工檢查目錄與行程狀態。雙平台驗收位於 `results/runtime-manual-resume/`。

### 離線診斷匯出

對已停止的模擬 BMC 執行以下指令。ZIP 包含 SQLite 一致性快照、開機與復原歷程、故障報告、摘要與 SHA-256 清單。匯出只收錄上述診斷檔案，憑證 token、TLS 私鑰與 UART 原始紀錄不會打包。清單可檢查內容是否變動。

```sh
python3 bmc-runtime/qemu/diagnostics.py export /path/to/appliance-run diagnostics.zip
python3 bmc-runtime/qemu/diagnostics.py verify diagnostics.zip
```

驗證會檢查檔案清單與雜湊、SQLite 完整性，以及摘要是否符合原始資料。Linux 與 ArceOS 的復役資料已匯出至 `results/runtime-diagnostic-export/`。

使用 `diagnostics.py compare linux.zip arceos.zip` 比對兩份診斷歸檔。工具先驗證兩份 ZIP，再比對開機歷程、故障原因、事件種類與來源、以及缺失事件區間。首次輪詢看到的重啟後序號單獨列出。退出碼 0 表示比對一致，2 表示有差異且報告會列出各類首個差異，1 表示歸檔無法通過驗證。比對結果位於 `results/runtime-diagnostic-export/comparison.json`。

## 一次執行雙平台發布驗收

驗收需先在執行用的 Python 環境安裝 `dbus-next==0.2.3`，並提供 `dbus-daemon`。分別使用 `run.sh` 建立一般映像和設定 `BMC_TEST_CONTROL_STALL=1` 的故障注入映像，兩組使用相同平台 commit，再執行：

```sh
python3 bmc-runtime/qemu/verify_release.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-release-guard-test-images results/runtime-release-openbmc \
  --release-images results/runtime-cli-cycle-images --dbus-daemon /path/to/dbus-daemon
```

入口依序跑 Runtime 功能、HTTPS 與憑證、整套服務故障復原與人工復役，接著對一般映像跑 HTTPS 與管理工具回歸，並透過 UART 送出停滯注入指令確認控制進度仍增加。一般映像也會在私有 D-Bus 驗證 OpenBMC 感測器探索、機箱關聯、主機電源控制與過溫拒絕。最後以本輪新產生的資料匯出並比對雙平台診斷。結果目錄保存映像雜湊、QEMU 版本、分階段狀態、紀錄與診斷 ZIP。任一階段失敗時回傳非零退出碼，已產生的驗收資料會保留。

### HTTPS 管理路徑效能

```sh
python3 bmc-runtime/qemu/benchmark_https.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-control-health-images results/runtime-https-performance
```

測試使用正式閘道與持續 SQLite 收集器，背景每秒提出 25 次已認證查詢。每個系統兩次開機、共 100 次 power-good 失效。管理查詢經 HTTPS，斷電延遲由主機注入 GPIO5 到讀回 GPIO0。CPU 分別記錄 QEMU、閘道與收集器，未計入負載產生器和啟動器。兩個平台各 100 次故障都歸檔，且均未超過 65 ms。QEMU CPU 時間 Linux／ArceOS 為 8.19／4.19 秒，三個行程加總為 12.69／12.66 秒。摘要、逐筆量測和資料庫位於 `results/runtime-https-performance/`。

HTTPS 閘道的成功 GET 可用 `Connection: keep-alive` 重用客戶端連線，每條最多 256 個請求。每筆仍獨立驗證 Bearer token，閘道到客體每次另建連線。加上 `benchmark_https.py --keep-alive` 可比較重用模式。25 Hz 下，各平台各模式的 100 次故障都完成斷電並歸檔。ArceOS 閘道 CPU 為 7.88 → 3.89 秒，三個服務行程加總為 12.48 → 9.01 秒。逐次建連試跑曾收到上游 502，對照重跑使用相同 Idempotency-Key 限次重試啟動；背景查詢維持單次。結果位於 `results/runtime-https-close/` 和 `results/runtime-https-reuse/`。

HTTPS 閘道可為重用 TLS 的 GET 保留後端 HTTP 連線，全閘道最多保留四條，其他請求仍可使用短連線。每筆轉送都帶原請求的 token 與強制驗證標頭。若既有後端連線已失效，閘道只會重新送出該次 GET；POST 使用短連線。雙平台測試驗證後端閒置逾時後同一 TLS 連線仍可讀取，以及重啟後的認證流程。25 Hz 量測位於 `results/runtime-https-upstream-reuse/`。Linux／ArceOS 各 100 筆故障均歸檔，超過 65 ms 分別為 0／1 次，服務 CPU 合計 10.33／9.18 秒。相對上一輪僅重用 TLS，Linux 合計下降，ArceOS 合計略增。

### Redfish 管理命令

```sh
python3 bmc-runtime/qemu/manage.py https://127.0.0.1:8443 \
  --ca-file /path/to/ca.pem --token-file /path/to/run/operator.token On
```

也可使用 `status`、`ForceOff` 或 `PowerCycle`。工具透過 Redfish 探索唯一主機，查詢任務後確認最終電源狀態。操作未達目標時會輸出任務及最近五筆日誌。每次提交會在 stderr 印出重送鍵。若回應遺失，可在原指令加上 `--idempotency-key` 並提供同一個鍵，適用範圍沿用 Runtime 的去重保留窗口。退出碼 0 為成功，2 為操作被拒或未完成，1 為連線、憑證或其他客戶端錯誤。雙平台結果位於 `results/runtime-management-client/`。

管理工具的 `PowerCycle` 要求 Redfish 任務的 `Oem.BmcG1.Event` 為 `power_cycle_scheduled`，並等待主機回到 `On`。同一重送鍵可在斷電等待或上電後取回原任務。雙平台 CLI 重啟驗收位於 `results/runtime-cli-cycle/`。

## OpenBMC D-Bus 感測器橋接

主機端橋接器用 Reader 憑證透過 HTTPS 讀取 BMC-G1 的狀態快照，並在指定的 D-Bus 上發布 `xyz.openbmc_project.BmcG1.Sensors`。客戶端可在 `/xyz/openbmc_project/sensors` 用 ObjectManager 探索進氣、板區溫度與風扇轉速，使用 `xyz.openbmc_project.Sensor.Value` 讀取攝氏或 RPM 值及接收變更訊號。資料過期或無法取得時，讀值為 NaN。`UpdatedTime=0` 表示來源未提供可用的取樣時間。

```sh
python3 -m pip install dbus-next==0.2.3
python3 bmc-runtime/qemu/openbmc_sensors.py https://127.0.0.1:8443 \
  --ca-file /path/to/ca.pem --token-file /path/to/reader.token --bus-address "$DBUS_SESSION_BUS_ADDRESS"
```

`verify_openbmc.py` 使用真正的私有 dbus-daemon 與兩種 QEMU 客體驗證讀取、過期訊號與唯讀權限。需 Python 3.9 以上、dbus-daemon 與 OpenSSL。這是主機端相容橋接，感測器透過 `Association.Definitions` 指向模擬 G1 機箱的 Inventory.Item。橋接器也在私有模擬匯流排提供 ObjectMapper 子集，支援 `GetObject`、`GetSubTree`、`GetSubTreePaths`、`GetAssociatedSubTree` 和 `GetAssociatedSubTreePaths`。機箱的 `sensors` 與各感測器的 `inventory` 可直接讀取 endpoints。此索引涵蓋橋接器發布的固定物件。啟動時若匯流排已有 ObjectMapper，橋接器會拒絕啟動。上游 ObjectMapper 與套件整合仍待開發。驗收結果位於 `results/runtime-openbmc-mapper/`。[OpenBMC Sensor.Value 定義](https://raw.githubusercontent.com/openbmc/phosphor-dbus-interfaces/master/yaml/xyz/openbmc_project/Sensor/Value.interface.yaml)

D-Bus 橋接也在 `/xyz/openbmc_project/state/host0` 與 `/xyz/openbmc_project/state/chassis0` 發布唯讀的 `CurrentHostState` 和 `CurrentPowerState`。啟動、重試及斷電重啟等待期間會映射為轉換中，power-good 確認後映射為 Running／On，斷電或鎖定後映射為 Off。狀態改變時送出 `PropertiesChanged`。HTTPS 讀取失敗時，`Availability.Available` 轉為 false，狀態欄保留最後讀值。機箱狀態透過 monitoring 關聯連到機箱清單。這些狀態由主機端輪詢模擬 Runtime，短於輪詢間隔的轉換可能不會被觀察到。雙平台驗收位於 `results/runtime-openbmc-power-state/`。

橋接器預設唯讀。在僅允許受信任操作者連線的私有 D-Bus 上，可加 `--operator-token-file /path/to/operator.token` 啟用 Host 的 `RequestedHostTransition`。支援 On、Off、Reboot，分別轉成 Runtime 的 start、stop、cycle。D-Bus 寫入成功代表控制迴路已處理請求，實際供電需以 CurrentHostState 和 power-good 確認。過溫拒絕回傳 NotAllowed。最後接受的 RequestedHostTransition 保存在橋接程序記憶體。雙平台驗收位於 `results/runtime-openbmc-control/`。

一鍵啟動可加上 `--dbus-daemon /path/to/dbus-daemon`，在主機建立獨立 D-Bus 並啟動唯讀 OpenBMC 橋接。若要在該私有匯流排操作電源，再加上 `--dbus-control`。啟動器使用的 Python 環境需安裝 `dbus-next==0.2.3`。客戶端可從 `ready.json` 取得 `dbus_address`，並等候橋接器的 `Availability.Available` 後讀取狀態。客體自動重啟期間，匯流排與橋接器保持運行，橋接器從更新後的檔案讀取憑證。人工復役會建立新的私有匯流排。

`verify_appliance.py` 加上相同的 `--dbus-daemon` 參數即可驗收 D-Bus 操作、客體重啟及憑證輪替。驗收結果位於 `results/runtime-appliance-dbus-final/`。

### 從固定版本建置並驗收

`release.py` 將兩套映像的建置和發布驗收串成單一指令。準備前述 Rust、QEMU、OpenSSL、D-Bus、Python 和 Alpine 輸入，並提供含指定 commit 的本機 AeroTensor-G1 Git 儲存庫。

```sh
python3 bmc-runtime/qemu/release.py /path/to/aerotensorg1 \
  /path/to/vmlinuz-virt /path/to/linux-virt.apk 6.18.60-0-virt /path/to/busybox-static.apk \
  results/runtime-release-build --qemu /path/to/qemu-system-aarch64 \
  --dbus-daemon /path/to/dbus-daemon
```

一般映像與故障注入映像各由暫用 checkout 建置。輸出含兩組映像、建置紀錄、輸入雜湊及 `acceptance/` 下的六階段結果。部署時指定 `ordinary/` 映像目錄。驗收工具所需的本機依賴和核心套件須先備妥。

### 可攜式模擬發布包

```sh
python3 bmc-runtime/qemu/package_release.py pack results/runtime-release-build \
  /path/to/vmlinuz-virt bmc-g1.zip
python3 bmc-runtime/qemu/package_release.py unpack bmc-g1.zip /path/to/new-release
```

打包會核對六階段驗收與映像雜湊。ZIP 包含一般映像、Linux 核心、啟動及管理腳本、依賴清單和驗收證據。解包會在建立輸出目錄前檢查清單與雜湊。部署可從解包目錄執行 `python3 qemu/appliance.py arceos /path/to/qemu-system-aarch64 vmlinuz-virt images /path/to/new-run`，並提供原有的憑證參數。QEMU、憑證與選用的 D-Bus 環境由部署端提供。SHA-256 清單用於完整性檢查。發布包與解包後的驗收位於 `results/runtime-distribution/`。

### 使用真正的 OpenBMC ObjectMapper 與 bmcweb

在隔離的 Romulus QEMU 參考客體啟動上游 OpenBMC。將 SSH 與 HTTPS 轉發限制在主機 loopback。用可信 SSH 金鑰連入該客體，再用以下指令將私有 Unix socket 接到客體的 system D-Bus。

```sh
python3 bmc-runtime/qemu/openbmc_bus_tunnel.py root@127.0.0.1 /path/to/private/bus --port 28222
```

`openbmc_sensors.py --external-mapper --sensors-only` 會交由上游 Mapper 管理物件探索及關聯。橋接器提供機箱和感測器，Romulus 的原生狀態服務繼續持有電源物件。上游 bmcweb 使用機箱的 `all_sensors` 關聯來發現三個感測器。

```sh
python3 bmc-runtime/qemu/verify_upstream.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt results/runtime-release-build/ordinary /path/to/new-results \
  --bus-address unix:path=/path/to/private/bus --bmcweb-port 28444 \
  --credentials /path/to/test-account --cert /path/to/test-cert.pem --key /path/to/test-key.pem \
  --insecure-test-bmcweb
```

測試帳號檔為單行 `username:password`，只用於隔離測試。橋接器驗證 BMC-G1 閘道憑證。此處的 `--insecure-test-bmcweb` 對應 Romulus 客體的預設測試憑證，若已設可信 CA，可改用 `--bmcweb-ca-file`。可加 `--validator /path/to/rf_service_validator --schema-directory /path/to/cached-schemas` 保存單一板區感測器資源的 DMTF 報告。驗收結果與 OpenBMC 版本及參考映像雜湊位於 `results/runtime-upstream-openbmc/`。

#### 使用上游 bmcweb 操作 BMC-G1 電源

在專用的 Romulus QEMU 測試客體內，暫時停用原生 Host0／Chassis0 電源狀態服務，讓橋接器取得 bmcweb 使用的兩個 D-Bus 服務名稱：

```sh
systemctl mask --runtime --now xyz.openbmc_project.State.Host@0.service \
  xyz.openbmc_project.State.Chassis@0.service
```

上游 ObjectMapper 與 bmcweb 繼續運行。依前一節建立私有 SSH 通道，並在 `verify_upstream.py` 加上 `--power`。橋接器會以獨立 D-Bus 連線註冊主機和機箱電源狀態，感測器仍由原橋接服務發布。若原生服務仍持有名稱，橋接器拒絕接管。測試結束後可重啟 Romulus 測試客體以清除暫時遮蔽。

驗收透過 bmcweb 的 ComputerSystem.Reset 執行 On、PowerCycle、ForceOff，核對 GPIO、power-good、Redfish 狀態與 Runtime 事件。過溫時，控制迴路阻擋上電，驗收使用的 bmcweb 將拒絕映射成 HTTP 405。結果位於 `results/runtime-upstream-power/`。本測試仍有上游查詢到未提供的 Asset 與 RebootAttempts 介面，這些欄位未納入本輪支援範圍。

#### 上游 Redfish 工作階段與 DMTF 資源驗收

在上述隔離 Romulus 環境執行 `verify_upstream.py --power --conformance --validator /path/to/rf_service_validator --schema-directory /path/to/cached-schemas`，其餘參數沿用上節。驗收會建立暫時 ReadOnly 帳號，測試 SessionService 登入、讀取、電源操作被拒、登出及 token 失效，再用管理員工作階段執行電源操作。測試結束會刪除暫時帳號與工作階段。

Service Validator 使用 Session 認證，分別驗證 ComputerSystem、G1 Chassis、SensorCollection、板區溫度、風扇與 SessionService。每份報告和退出碼各自保存，以便追蹤差異。上游模式的感測器物件使用 `g1_` 前綴，避免與 Romulus 原生 hwmon 路徑重名。結果位於 `results/runtime-upstream-conformance-final/`。本次範圍涵蓋選定資源的 schema 與實際權限、電源操作情境。

#### DMTF Redfish Protocol Validator

在專用 Romulus 測試客體，於上節完整指令再加入 `--protocol-validator /path/to/rf_protocol_validator`。它會在每個 BMC-G1 後端仍已連線時執行原版 DMTF 測試，保留 HTML、TSV 和機器可讀摘要。工具會操作測試帳號與工作階段，因此驗收應使用可丟棄的 QEMU 映像副本。測試只轉發 HTTPS，工具以 `--avoid-http-redirect` 執行。使用隔離測試憑證時沿用原指令的 `--insecure-test-bmcweb`。

`upstream.json` 分別保存電源操作、權限、資源 schema 與協定結果。協定測試的非零退出碼會保留在報告中，驗收仍會繼續測試第二個後端。若工具未產出摘要，驗收會中止並保留工具日誌。

本輪使用 Protocol Validator 1.3.2，兩個後端皆為 395 通過、1 警告、0 失敗、32 未測。警告為帳號資源回傳 ETag HTTP 標頭但缺少 `@odata.etag`。並行條件式 PATCH 測試觀察到連線錯誤，HTTP 到 HTTPS 轉址也未測。報告位於 `results/runtime-upstream-protocol/`。

#### BMC-G1 Redfish 互通性 profile

在上述 Romulus 驗收指令加入 `--interop-validator /path/to/rf_interop_validator`，並沿用 `--power --conformance --validator ... --schema-directory ...`。預設採用專案的 `profiles/BmcG1Management.v1_0_0.json`，也可用 `--interop-profile` 指定其他 profile。測試涵蓋服務與主機探索、Reset 宣告及支援值、G1 機箱、三個指定感測器與 SessionService。Interop Validator 僅讀取 Redfish，開機、斷電重啟、關機、過溫拒絕與角色授權由同一驗收入口的功能測試核對。

兩個後端各通過 34 項，零警告、零失敗、零未測，範圍是 BMC-G1 專案 profile。工具完成後會保存雙平台報告，若 profile 有失敗則回傳非零。原始報告與 profile 雜湊位於 `results/runtime-upstream-interop-profile/`。

#### 原生 phosphor-state-manager 協作

使用專用的 Romulus QEMU 映像副本。保留原生 Host0／Chassis0 狀態服務，並在客體執行 `prepare_romulus_state_manager.sh`，暫停原本的 GPIO 電源服務及只適用於實體 OpenPOWER 的硬體單元。該設定寫入 `/run`，重啟後清除。建立私有 SSH D-Bus 通道後，以 `verify_upstream.py --native-state-manager` 取代原有的 `--power` 參數。

此模式下原生狀態管理器啟動 systemd 電源流程。橋接器在 `org.openbmc.control.Power` 接收 `setPowerState`，並把 BMC-G1 已確認的電源狀態回傳為 `pgood` 與變更訊號。本輪驗證 bmcweb 的 On、ForceOff、虛擬 GPIO、Runtime 事件及原生 Redfish 最終電源狀態。原生 PowerCycle 與 DMTF profile 已在下一節完成回歸。異常斷電後的原生自動恢復策略仍待驗收。結果位於 `results/runtime-upstream-native-state/`。

#### 原生狀態管理器的斷電重啟與互通性

在專用 Romulus 客體執行 `prepare_romulus_state_manager.sh` 並建立前述私有 SSH D-Bus 通道後，將 `--native-state-manager --conformance --validator /path/to/rf_service_validator --schema-directory /path/to/cached-schemas --interop-validator /path/to/rf_interop_validator` 加到 `verify_upstream.py`。測試用管理員工作階段執行 On、PowerCycle、ForceOff。PowerCycle 必須先觀察到 GPIO0 低電位，再等 GPIO0 重新拉高並注入新的 power-good。這條路徑由原生 systemd 流程分別下達停止與啟動指令，因此事件序列包含兩次 `power_started`／`power_on`／`power_stopped`。驗收也比對 Redfish 最終電源狀態、只讀角色拒絕操作及工作階段登出。結果位於 `results/runtime-upstream-native-profile/`。

#### 原生模式完整 Redfish 驗收

沿用原生模式的 Romulus 準備方式與 D-Bus 通道，對同一輪驗收同時指定三套 DMTF 工具：

```sh
python3 bmc-runtime/qemu/verify_upstream.py /path/to/qemu-system-aarch64 \
  /path/to/vmlinuz-virt /path/to/ordinary-images /path/to/new-evidence \
  --bus-address unix:path=/path/to/private/bus --bmcweb-port 28444 \
  --credentials /path/to/test-account --cert /path/to/gateway-cert.pem \
  --key /path/to/gateway-key.pem --insecure-test-bmcweb \
  --native-state-manager --conformance \
  --schema-directory /path/to/cached-schemas --validator /path/to/rf_service_validator \
  --interop-validator /path/to/rf_interop_validator \
  --protocol-validator /path/to/rf_protocol_validator
```

同一次執行依序核對原生電源流程、Session 權限、六項資源、HTTP 協定及 BMC-G1 profile。兩個後端各有 `upstream.json` 與原始驗證報告。協定測試會操作帳號和工作階段，請使用隔離且可丟棄的 Romulus 映像副本。此路徑仍以 QEMU 虛擬電源與模擬回饋做驗收。

雙後端摘要和原始報告位於 `results/runtime-upstream-native-protocol/`。驗收程式會先寫出 `comparison.json`，再依兩個後端的驗證結果回傳退出碼。任何已啟用的驗證器失敗都會使程式回傳非零。警告及未測項目可在摘要與原始報告中查閱。

Linux 與 ArceOS 都完成 On、PowerCycle、ForceOff，電源事件序列及 Redfish 最終狀態一致。兩者都通過六項資源驗證與 34 項 BMC-G1 profile 斷言。

| 後端 | 協定通過 | 警告 | 失敗 | 未測 |
| --- | ---: | ---: | ---: | ---: |
| Linux | 395 | 1 | 1 | 31 |
| ArceOS | 395 | 1 | 0 | 32 |

兩輪共用同一個 OpenBMC bmcweb，皆觀察到帳號資源只有 HTTP ETag 標頭，缺少 `@odata.etag`。Linux 輪次對同一帳號送出四個帶相同 If-Match 的並行 PATCH，四筆都收到 204，重現條件更新的原子性問題。ArceOS 輪次的四筆請求都因連線錯誤或逾時未完成，驗證器將該項列為未測。整輪驗收的結果為失敗。

#### 原生整合收尾回歸

沿用前述 Romulus 指令，加上 `--account-etag --native-power-loss`，可獨立重跑這兩項。若只跑收尾檢查，可省略 `--protocol-validator`。針對掉電回歸，在專用 Romulus 客體將 `PowerRestorePolicy` 設為 `AlwaysOn`：

```sh
busctl set-property xyz.openbmc_project.Settings \
  /xyz/openbmc_project/control/host0/power_restore_policy \
  xyz.openbmc_project.Control.Power.RestorePolicy PowerRestorePolicy s \
  xyz.openbmc_project.Control.Power.RestorePolicy.Policy.AlwaysOn
```

帳號檢查使用每輪建立並刪除的臨時帳號。先核對無效 ETag 被拒絕，再以四條已建立的 TLS 連線同步送出相同 `If-Match`。判定條件為一筆 204、三筆 412，且事後資源已更新並取得新 ETag。連線失敗留在報告中，PATCH 不會重送。

掉電檢查在 PowerCycle 後持續拉低虛擬 power-good，核對 Runtime 事件與 GPIO，再觀察 Redfish 的 `Off` 回報及是否自動重新啟動。此情境模擬主機電源回饋中斷。PSU 輸入狀態由失電轉為供電的路徑仍未驗證。

最終重跑中，兩個後端均關閉 GPIO，且各記錄一次 `power_good_lost`。兩輪的 Redfish 均維持 `On`，在觀察窗內也未見自動重啟。帳號檢查在兩輪都重現四筆 204。兩輪仍各通過六項資源驗證及 34 項 BMC-G1 profile 斷言，程式保存報告後以狀態碼 1 結束。最終摘要位於 `results/runtime-upstream-closeout-recheck/comparison.json`，首次收尾的 TLS 連線重設記錄位於 `results/runtime-upstream-closeout/comparison.json`。

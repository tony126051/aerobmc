# QEMU 虛擬 GPIO、IRQ 與逾時驗證

## 共用電源狀態機

電源狀態機已移入 `axbmc-core`，確定性重播與 Linux／ArceOS 的 PL061 客體都呼叫同一份實作。GPIO0 反映狀態機的使能輸出，GPIO3 提供 IRQ 時間戳與處理當下的電位。客體的控制任務依序處理 IRQ 與逾時。

整合驗證採用 100 ms 期限與一次啟動嘗試。每種情境在 Linux 與 ArceOS 各啟動三次，共 12 次全部通過。

| 情境 | 主機讀回的 GPIO0 與客體狀態 |
|---|---|
| 期限前注入 power-good | 啟動時 GPIO0 為高。收到高電位回饋後進入 `On`，原期限到達時仍維持高電位，逾時計數為零 |
| 到期後注入 power-good | GPIO0 先高後低。主機確認已拉低再注入 IRQ，客體確認收到事件並維持 `Failed`，GPIO0 保持低電位 |

共用狀態機的重試及舊事件過濾由確定性重播驗證。Linux 與 ArceOS 各開機三次，每次 19 個案例，共 114 筆符合預期。PL061 整合案例驗證單次啟動。建置記錄、序列埠輸出與逐次結果位於 `results/qemu-power-integrated/` 與 `results/qemu-shared-power-replay/`。

## 風扇 tach 失聯整合

風扇保護已移入 `axbmc-core`，由確定性重播及 Linux／ArceOS 的 PL061 客體共用。風扇狀態機在連續失聯達期限時將設定值由 50% 提升為 100%。期限前收到有效轉速會重設失聯計時。保護觸發後維持保護設定。

主機在 GPIO3 注入一次 tach 上升緣，接著停止送出脈波。客體以該 IRQ 的時間戳起算 100 ms，透過計時器喚醒並更新狀態機。GPIO0 對應保護狀態，主機讀回輸出並確認客體報告的保護時間未早於期限。這個測試驗證失聯到保護輸出的路徑，PWM 波形及 RPM 換算仍屬後續模擬項目。

Linux 與 ArceOS 各開機三次，六次均通過。從 tach 擷取到 GPIO0 寫入完成的範圍為 Linux 104.971–105.241 ms、ArceOS 101.591–111.734 ms。這些數字包含 100 ms 的測試期限。兩個電源情境也各在兩個客體開機三次並通過。完整結果位於 `results/qemu-fan-integrated/`。

## 初次 GPIO 路徑量測

使用 QEMU 10.2.2 的 AArch64 `virt`、GICv2、TCG、Cortex-A72、單 vCPU 及 256 MiB RAM。QMP 的 `system_powerdown` 走 QEMU 既有的虛擬按鍵線，觸發 PL061 GPIO3 上升緣。測試把 GPIO3 當作 power-good 事件來源，GPIO0 當作控制輸出。主機透過 QMP 讀取 GPIO0 的 MMIO 值，並核對客體輸出的時間戳和結果。[QEMU virt 原始碼](https://github.com/qemu/qemu/blob/v10.2.2/hw/arm/virt.c)

Linux 使用 Alpine `6.18.60-0-virt`、同版本的 `gpio-pl061.ko` 和 GPIO character device v1。ArceOS 使用 AeroTensor-G1 commit `39220889fae5184885778feb222afa30be18c986`，由 GIC INTID 39 的 IRQ handler 擷取事件、清除中斷並喚醒控制任務。[Linux GPIO ABI](https://docs.kernel.org/userspace-api/gpio/chardev_v1.html)

| 驗證 | 主機檢查 | Linux | ArceOS |
|---|---|---:|---:|
| 正常 IRQ | 注入前 GPIO0 為低。客體處理後 GPIO0 為高 | 3/3 | 3/3 |
| 逾時後送達的 IRQ | GPIO0 先拉高，100 ms 後由客體計時器路徑拉低。主機確認低電位再注入 IRQ，並確認客體收到事件且 GPIO0 保持低 | 3/3 | 3/3 |

| 客體內計時 | Linux 中位數 | ArceOS 中位數 |
|---|---:|---:|
| 正常 IRQ 擷取到輸出寫入完成 | 1.373 ms | 0.553 ms |
| 逾時門檻到輸出拉低完成 | 2.893 ms | 2.137 ms |

每格為三次獨立開機的中位數。Linux 的正常路徑從 kernel GPIO event 的時間戳量起，ArceOS 從 IRQ handler 進入量起。兩者時間戳的取樣點不同，以上數字供追蹤各路徑。逾時路徑由客體的單調時鐘及排程計時器執行，100 ms 為測試設定。此處的輸出控制使用共用的簡化狀態，完整的 19 個邊界與重試情境繼續由 [確定性重播](qemu-control.zh-TW.md) 驗證。

## 一鍵重跑

使用 [既有工具鏈步驟](qemu-replay.zh-TW.md) 安裝 Rust nightly 及 ArceOS 建置工具，並準備新的 G1 checkout。Linux kernel 和 PL061 module 必須是同一版。此次 module 取自 Alpine v3.23 AArch64 的 `linux-virt-6.18.60-r0.apk`，路徑為 `lib/modules/6.18.60-0-virt/kernel/drivers/gpio/gpio-pl061.ko.gz`，解壓後傳入腳本。

```sh
sh tools/gpio-irq/run.sh /path/to/disposable-g1 \
  /path/to/vmlinuz-virt /path/to/gpio-pl061.ko results/gpio-validation
```

可透過 `QEMU_BIN` 指定 QEMU 路徑。腳本會在暫用 checkout 加入驗證程式，建置電源正常啟動、電源逾時及風扇失聯三種映像，再讓每個客體對各情境開機三次。輸出目錄須為新路徑，會保存 CSV、序列埠 log、編譯器與 commit 資訊、映像及 SHA-256。初次 GPIO 驗證位於 `results/qemu-gpio-final/`。

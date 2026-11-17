# BMC-G1 板級規格與實機整合資料

本文是 AeroBMC 最小可行核心的板級參考。實體板相關內容屬早期研究，實作與交付驗收範圍為 QEMU 模擬。 [專案定位](../README.md)

目前專案採全軟體模擬。本文保留先前公板與實機研究供參考。現行驗收目標為 QEMU AArch64 `virt`，Pi 4B 映像用於補充的 `raspi4b` 開機測試。後續虛擬周邊的訊號、IRQ 與時序由模擬規格定義。

實體目標板、PCB revision、管理處理器與 BSP 版本待確認。下方規格表保留每項驅動所需的欄位及對應來源，供取得板卡資料後填寫與驗證。

## 目前有來源的資料

| 項目 | 已確認範圍 |
|---|---|
| AeroTensor-G1 | 上游把 G1 定義為架構研究原型，現有規格來自模擬、模型與目標設計。[上游 README](https://github.com/tony126051/aerotensorg1/blob/39220889fae5184885778feb222afa30be18c986/README.md) |
| G1 QEMU 裝置 | `aerotensor-soc` 實作 NPU MMIO 與 16 條 NPU 中斷輸出。其 NPU reset／enable 暫存器控制加速器。[QEMU 裝置原始碼](https://github.com/tony126051/aerotensorg1/blob/39220889fae5184885778feb222afa30be18c986/qemu/hw/misc/aerotensor_soc.c) · [NPU 暫存器規格](https://github.com/tony126051/aerotensorg1/blob/39220889fae5184885778feb222afa30be18c986/docs/03_custom_ai_chip_integration.md) |
| BMC-G1 模擬目標 | 本專案已在 AArch64 QEMU `virt` 驗證 Linux 和 ArceOS。風扇、轉速、電源使能、power-good 及上升緣時間戳由測試程式模擬。13 ms 輪詢、39 ms power-good 期限、26 ms 重試間隔、最多三次嘗試和 100% 保護 PWM 均為測試參數。[QEMU 控制驗證](qemu-control.zh-TW.md) |

已查閱 G1 上游固定 commit 的架構文件、QEMU 裝置及啟動腳本，尚未找到 G1 BMC 板卡原理圖、PCB revision、GPIO／PWM 映射或電源時序。上游另有 Phytium Pi 的 DTS，其資料對應該平台。[Phytium Pi DTS](https://github.com/tony126051/aerotensorg1/blob/39220889fae5184885778feb222afa30be18c986/arceos/tools/phytium_pi/phytium.dts)

## 實體目標與驅動資料

可用 **Raspberry Pi 4 Model B** 作為首個實機驗證目標。G1 分支已有 Raspberry Pi 4 的 ArceOS 建置說明，板卡的 40-pin GPIO 可接風扇與外部回饋測試治具。建議先在同一塊板跑 AArch64 Linux 與 ArceOS，再驗證 PWM、tach 與模擬 power-good 的 GPIO IRQ。仍需確認實板 revision、腳位複用、電氣介面和 ArceOS 驅動。Yocto 有 `raspberrypi4-64` BSP，完整 OpenBMC 映像與板級服務需另行驗證。[G1 的 ArceOS 建置說明](https://github.com/tony126051/aerotensorg1/blob/39220889fae5184885778feb222afa30be18c986/arceos/README.md) · [Raspberry Pi 4 官方規格](https://pip-assets.raspberrypi.com/categories/545-raspberry-pi-4-model-b/documents/RP-008341-DS-1-raspberry-pi-4-datasheet.pdf?disposition=inline) · [Yocto BSP 設定](https://github.com/agherzan/meta-raspberrypi/blob/master/conf/machine/raspberrypi4-64.conf)

Pi 4 參考映像已建置，並在 QEMU `raspi4b` 完成開機與重播。測試治具腳位及實板驗收見 [Pi 4 參考公板](raspi4-reference.zh-TW.md)。

若優先驗證原版 OpenBMC，AST2600 EVB 有上游 machine 設定與 DTS。其主核心是 32 位元 Cortex-A7，無法沿用本專案的 AArch64 ArceOS 映像。[OpenBMC EVB 設定](https://github.com/openbmc/openbmc/blob/master/meta-evb/meta-evb-aspeed/meta-evb-ast2600/conf/machine/evb-ast2600.conf) · [ASPEED AST2600 規格](https://www.aspeedtech.com/tw/server_ast2600/)

| 所需項目 | 要從板級資料取得的值 | 優先來源 | 目前狀態 |
|---|---|---|---|
| 板卡與執行位置 | 板名、PCB revision、管理 SoC／核心、ArceOS 啟動目標、BSP commit；管理端與 G1 的電源域及控制權 | 板卡標籤、硬體規格、BSP | 待確認 |
| 電源輸出 `POWER_ENABLE` | 原理圖 net 名、控制器及線號、pinmux、有效極性、電壓域、pull、開機／reset 預設狀態、對應電源軌 | 原理圖、DTS／pinmux、SoC 手冊 | 待確認 |
| 回饋輸入 `POWER_GOOD` | 原理圖 net 名、回報電源軌、控制器及線號、有效極性、電壓域、pull、濾波／debounce、是否合併多軌回饋 | 原理圖、電源控制器手冊、DTS | 待確認 |
| Power-good IRQ 與時鐘 | 支援邊緣、IRQ 路由、pending 清除方式、是否有硬體時間戳、時鐘解析度與時鐘域、跨重試的事件歸屬 | SoC／GPIO 手冊、DTS interrupt specifier、BSP | 待確認 |
| 電源時序 | 啟動順序、每軌最大上升時間、power-good 必須有效的期限、逾時後輸出切斷期限、放電／重試間隔、嘗試上限、掉電策略 | 電源時序圖、板規、電源控制器手冊 | 待確認 |
| 風扇 PWM | 接頭、控制器與 channel、頻率、極性、最小啟動與運轉 duty、保護 duty、開機時的硬體行為、控制權 | 原理圖、風扇手冊、DTS／BSP | 待確認 |
| 風扇 tach | 接頭、回饋線與 channel、每轉脈波數、計數／擷取方式、最低有效轉速、失聯與恢復門檻 | 原理圖、風扇手冊、DTS／BSP | 待確認 |
| 溫度與電源感測 | 元件料號、匯流排／mux、位址、暫存器或驅動、量測單位與比例、讀取週期、門檻與失效行為 | 原理圖、BOM、資料手冊、DTS／BSP | 待確認 |
| 實機啟動與量測 | 開機媒體、console、韌體載入方式、測試點、可量測的 enable／good／PWM／tach 訊號 | 板卡使用手冊、原理圖、BSP | 待確認 |

`POWER_ENABLE` 與 `POWER_GOOD` 是本文的功能名稱。收到資料後，每列記錄板卡 revision、檔案版本、原理圖頁碼／net、DTS 節點或手冊章節，並區分文件宣告值與實測值。

## 取得資料後的板級驗收

1. **確認板卡與控制權。** 核對 schematic、BSP 與實板 revision。確認誰負責電源切換、風扇控制，以及管理端在 G1 斷電後是否仍可執行。
2. **先量測電源輸出與回饋。** 在測試點核對極性、開機預設值、啟用到 power-good 的時間、power-good 到 IRQ／擷取的延遲，以及逾時到輸出關閉的時間。分別記錄回饋出現期限與實際切斷期限。
3. **核對中斷與期限競爭。** 若硬體能擷取邊緣時間戳，確認擷取時鐘與控制期限可比較，並驗證清除 pending、停用後收到事件及重試時的行為。若只能輪詢，量測最長輪詢間隔並納入電源時序預算。
4. **核對風扇與感測。** 量測 PWM 頻率與 duty、tach 脈波數與 RPM 換算。拔除 tach 或注入等效故障，量測從失聯到板規指定保護輸出的時間。最後核對感測器單位、讀取失敗與事件輸出。

啟動板級填寫的最小資料包為**板名與 revision、相關原理圖頁或等效 pin map、DTS／BSP、電源時序圖及風扇料號**。腳位資料能先完成映射，但期限和保護值需對應板規與元件資料。

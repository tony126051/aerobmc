# AeroBMC

AeroBMC 是一項局部架構實驗，探索如何精簡 OpenBMC 底層管理路徑的軟體層次，並改善時間敏感工作的反應延遲與抖動。實作範圍限定為在 QEMU 中執行的 Rust 最小可行核心（minimal viable core），以 ArceOS 為優先後端、Linux 為比較基準。整合驗證沿用上游 OpenBMC 的 ObjectMapper、bmcweb 與 phosphor-state-manager。完整 OpenBMC 發行版的功能與硬體部署超出本實驗範圍。

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

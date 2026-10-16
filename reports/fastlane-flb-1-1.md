# 快车道承认台账（批次 flb-1-1）

- 生成时间：2026-10-17 03:03:01
- 请求总数：2
- 状态计数：已承认（可直跑） 1；已升级回完整设计回合 1
- 口径：D5 §3.2（`batch_id` / `decided_by` / `decided_at` 来自 `fastlane_requests` 落库；`ESCALATED`/`REJECTED` 不许被承认）

| 请求号 | 轮次 | 状态 | 批次 | 承认人 | 承认时间 | 摘要 | 改动文件 |
|---|---|---|---|---|---|---|---|
| fl-1-1 | 1 | 已承认（可直跑） | flb-1-1 | jiemian | 2026-10-17 03:02:58 | 第 1 轮快车道登记（无任务包，仅登记） | src/kfpa/gates.py |
| fl-2-1 | 2 | 已升级回完整设计回合 | - | - | - | 第 2 轮快车道登记（无任务包，仅登记） | src/kfpa/schemas.py、src/kfpa/pipeline.py |

> 本报告由 `kfpa.ui.fastlane_page` 渲染；判据本身由 `kfpa.impact` 出（D3 §3.6）。

# 快车道承认台账（批次 demo）

- 生成时间：2026-10-17 01:42:33
- 请求总数：3
- 状态计数：已承认（可直跑） 1；已升级回完整设计回合 1；待承认 1
- 口径：D5 §3.2（`batch_id` / `decided_by` / `decided_at` 来自 `fastlane_requests` 落库；`ESCALATED`/`REJECTED` 不许被承认）

| 请求号 | 轮次 | 状态 | 批次 | 承认人 | 承认时间 | 摘要 | 改动文件 |
|---|---|---|---|---|---|---|---|
| fl-1-1 | 1 | 已承认（可直跑） | demo | jiemian | 2026-10-16 19:26:40 | 给 G7 报告加一列未覆盖行数（不动接口、不动数据模型，只碰 gates） | src/kfpa/gates.py |
| fl-1-2 | 1 | 待承认 | - | - | - | 补一条 release 快照的打印（只碰 ledger） | src/kfpa/ledger.py |
| fl-2-1 | 2 | 已升级回完整设计回合 | - | - | - | 想给 TaskPackage 加字段 → 碰数据模型 | src/kfpa/gates.py |

> 本报告由 `kfpa.ui.fastlane_page` 渲染；判据本身由 `kfpa.impact` 出（D3 §3.6）。

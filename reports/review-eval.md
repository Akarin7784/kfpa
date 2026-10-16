# KFPA 同源盲区度量报告（P2 / A21）

- 种子集：`seed.jsonl`　样本数：**24**
- 异构 provider：`mock_hetero`　同源 baseline：`mock`
- 生成时间：`2026-10-16 23:57:43`　契约版本：`1.3`

## 指标对照（异构 REVIEWER vs 同源 baseline）

| 指标 | 异构 | 同源基线 | 判据 | 结果 |
|---|---|---|---|---|
| 漏检 missed | 0 | 12 | 不增 | ✅ |
| 误报 false_alarms | 0 | 12 | 不增 | ✅ |
| 一致率 agreement | 1.000 | 0.500 | 不降 | ✅ |
| 样本数 total | 24 | 24 | 同批种子 | = |

## 结论

通过：漏检 0 ≤ 基线 12（✅）、误报 0 ≤ 基线 12（✅）、一致率 1.000 ≥ 基线 0.500（✅）

## 漏检样本（0）

（无）

## 误报样本（0）

（无）

## 全部样本（24）

| 样本 | 功能 | 人工 | 异构判定 | 同源判定 | 问题 | 缺陷 |
|---|---|---|---|---|---|---|
| S001 |  | fail | fail | - | — | AC2／major／[missing_error_code] 接口契约只写了成功返回，没写错误码／任务包 |
| S002 |  | fail | fail | - | — | AC3／major／[missing_boundary] qty 上限与精度未定义／任务包 |
| S003 |  | fail | fail | - | — | AC4／major／[idempotency_missing] 幂等键未定义，重放会重复扣减／任务包 |
| S004 |  | fail | fail | - | — | AC1／major／[untestable_acceptance] 「响应要快」不可测试，没有阈值／任务包 |
| S005 |  | fail | fail | - | — | AC5／major／[contract_ambiguity] order_time 时区未定义／任务包 |
| S006 |  | fail | fail | - | — | AC6／major／[prohibition_conflict] 禁止改库存表，但实现路径要求新增库存列／任务包 |
| S007 |  | fail | fail | - | — | AC2／major／[feedback_ignored] 上一轮失败原因未在禁止事项中体现／任务包 |
| S008 |  | fail | fail | - | — | AC3／major／[concurrency_missing] 并发提交同一 SKU 的锁策略未写／任务包 |
| S009 |  | fail | fail | - | — | AC4／major／[unit_undefined] 数量单位（件/kg）未定义／任务包 |
| S010 |  | fail | fail | - | — | AC1／major／[permission_undefined] 未说明接口权限校验要求／任务包 |
| S011 |  | fail | fail | - | — | AC5／major／[rollback_undefined] 失败回滚粒度与补偿方式未定义／任务包 |
| S012 |  | fail | fail | - | — | AC6／major／[timezone_ambiguous] 对账时点未给时区／任务包 |
| S013 |  | pass | pass | - | — | - |
| S014 |  | pass | pass | - | — | - |
| S015 |  | pass | pass | - | — | - |
| S016 |  | pass | pass | - | — | - |
| S017 |  | pass | pass | - | — | - |
| S018 |  | pass | pass | - | — | - |
| S019 |  | pass | pass | - | — | - |
| S020 |  | pass | pass | - | — | - |
| S021 |  | pass | pass | - | — | - |
| S022 |  | pass | pass | - | — | - |
| S023 |  | pass | pass | - | — | - |
| S024 |  | pass | pass | - | — | - |

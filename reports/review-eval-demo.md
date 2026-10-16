# KFPA 同源盲区度量报告（P2 / A21）

- 种子集：`benchmarks/review/seed.jsonl (demo 合成样本，非真实度量)`　样本数：**4**
- 异构 provider：`mock_hetero`　同源 baseline：`mock`
- 生成时间：`2026-10-16 19:26:40`　契约版本：`1.3`

## 指标对照（异构 REVIEWER vs 同源 baseline）

| 指标 | 异构 | 同源基线 | 判据 | 结果 |
|---|---|---|---|---|
| 漏检 missed | 1 | 2 | 不增 | ✅ |
| 误报 false_alarms | 1 | 1 | 不增 | ✅ |
| 一致率 agreement | 0.750 | 0.500 | 不降 | ✅ |
| 样本数 total | 4 | 4 | 同批种子 | = |

## 结论

通过：漏检 1 ≤ 基线 2（✅）、误报 1 ≤ 基线 1（✅）、一致率 0.750 ≥ 基线 0.500（✅）

## 漏检样本（1）

| 样本 | 功能 | 人工 | 异构判定 | 同源判定 | 缺陷 |
|---|---|---|---|---|---|
| S001 | order-atp | fail | pass | - | AC2／major／缺错误码／段4 接口契约 |

## 误报样本（1）

| 样本 | 功能 | 人工 | 异构判定 | 同源判定 | 缺陷 |
|---|---|---|---|---|---|
| S002 | stock-lock | pass | fail | - | AC5／major／误判幂等键／段2 验收标准 |

## 全部样本（4）

| 样本 | 功能 | 人工 | 异构判定 | 同源判定 | 问题 | 缺陷 |
|---|---|---|---|---|---|---|
| S001 | order-atp | fail | pass | - | 漏检 | AC2／major／缺错误码／段4 接口契约 |
| S002 | stock-lock | pass | fail | - | 误报 | AC5／major／误判幂等键／段2 验收标准 |
| S003 | picking | fail | fail | - | — | AC1／major／单位口径错／段4 |
| S004 | delivery | pass | pass | - | — | - |

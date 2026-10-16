# KFPA 同源盲区度量报告（P2 / A21）

- 种子集：`benchmarks/review/seed.jsonl (demo 合成样本)`　样本数：**4**
- 契约版本：`1.3`

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
| S001 | order-atp | fail | pass | pass | AC2／missing_error_code／段4 接口契约 |

## 误报样本（1）

| 样本 | 功能 | 人工 | 异构判定 | 同源判定 | 缺陷 |
|---|---|---|---|---|---|
| S002 | stock-lock | pass | fail | pass | - |

## 全部样本（4）

| 样本 | 功能 | 人工 | 异构判定 | 同源判定 | 问题 | 缺陷 |
|---|---|---|---|---|---|---|
| S001 | order-atp | fail | pass | pass | 漏检 | AC2／missing_error_code／段4 接口契约 |
| S002 | stock-lock | pass | fail | pass | 误报 | - |
| S003 | picking | fail | fail | fail | — | AC1／wrong_unit |
| S004 | delivery | pass | pass | pass | — | - |

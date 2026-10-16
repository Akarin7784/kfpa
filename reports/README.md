# reports/ —— 度量报告渲染产物（P2 / 界面）

本目录是 **渲染产物**（D4 §1 把 `reports/**` 划给界面的写者范围），不是数据源。

## 正式链路

```powershell
$env:PYTHONPATH="src"

# 1) 长流 eval.py 跑出度量 JSON（种子集 → 四项指标）
python -m kfpa eval review --out reports/review-eval.json

# 2) 界面渲染三种格式（任选）
python -m kfpa.ui.eval_page --in reports/review-eval.json --format markdown --full
python -m kfpa.ui.eval_page --in reports/review-eval.json --format html     --full
python -m kfpa.ui.eval_page --in reports/review-eval.json --format json     --full

# 3) 可选报告页（不列入验收）
$env:KFPA_EVAL_JSON="reports/review-eval.json"
python -m streamlit run src/kfpa/ui/eval_page.py
```

- 不给 `--out` 时输出名与输入同名、后缀随格式（`.md` / `.html` / `.json`）。
- 退出码：`0` 成功 / `2` 参数与 JSON 解析错 / `4` 输入文件不存在或缺少 streamlit / `5` 写文件失败。

## 输入形状（D4 §3.4 已冻结）

正式形状（`kfpa eval review --out` 的输出，长流生产）：

```json
{
  "corpus": "benchmarks/review/seed.jsonl",
  "generated_at": 1792150000.0,
  "provider": "mock_hetero",
  "baseline_provider": "mock",
  "metrics": { "corpus": "…", "total": 24, "missed": 1, "false_alarms": 0, "agreement": 0.958,
               "baseline_missed": 3, "baseline_false_alarms": 1, "baseline_agreement": 0.833 },
  "samples": [ { "id": "S001", "human_verdict": "fail", "review_verdict": "pass",
                 "missed": true, "false_alarm": false,
                 "findings": [ { "severity": "major", "location": "段4", "issue": "…", "criterion": "AC2" } ] } ]
}
```

- `metrics` 字段名与 `schemas.ReviewMetrics` 一一对应；**结论（通过/不通过）由渲染层用 `passed()` 判**，报告不写死。
- `samples` 每条**自带 `missed` / `false_alarm` 布尔**，渲染层**不得自行推断**；缺布尔时标「未标注」并在报告里给提示。
- **缺 `baseline_*` 时判「无法判定」**，不按 0 假设。
- 另为兼容保留：`schemas.ReviewMetrics` 实例、扁平指标 dict、`{"groups": {"hetero": …, "baseline": …}, "samples": […]}`。

## 本目录文件

| 文件 | 说明 |
|---|---|
| `review-eval-demo.json` | **合成示例**输入（4 条样本：1 漏检 / 1 误报 / 2 正确），用于离线自测渲染链路 |
| `review-eval-demo.md` | 由上面渲染的 Markdown 报告（示例） |
| `review-eval-demo.html` | 由上面渲染的**单文件 HTML**（内联 CSS、无 CDN/外链/脚本） |

`review-eval.json|md|html`（无 `-demo`）是正式产物，由长流 `kfpa eval review` 跑出后渲染，
不在本目录预先提交（避免把合成数据当真实度量）。

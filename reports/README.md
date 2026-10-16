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
| `review-eval.json` | **正式产物**：`kfpa eval review` 真跑（`provider=mock_hetero` vs `baseline=mock`，24 条种子集）输出的 D4 冻结形状 JSON |
| `review-eval.md` / `.html` | 由上面这份正式 JSON 渲染的 Markdown 报告与**单文件 HTML**（内联 CSS、无 CDN/外链/脚本） |
| `review-eval-demo.json` | **合成示例**输入（4 条样本：1 漏检 / 1 误报 / 2 正确），用于离线自测渲染链路 |
| `review-eval-demo.md` / `.html` | 由合成示例渲染的报告（自测用） |
| `review-eval-mock.json` | **链路证据**：`provider=mock`（两组同源、指标退化）时的生产者输出，留作对照 |
| `review-eval-mock.md` / `.html` | 由上面那份退化输入渲染的报告 |

正式产物（`review-eval.*`）本次实测：

```
样本 24｜漏检 0（baseline 12）｜误报 0（baseline 12）｜一致率 100.00%（baseline 50.00%） → 达标
provider=mock_hetero / baseline=mock　渲染层 passed()=True
```

生成方式（生产端在合流树里跑，渲染端在本 clone）：

```powershell
# 生产端（router + review 的本地合流树；工作目录即其仓库根）
$env:PYTHONPATH="src"
python -m kfpa eval review --out reports/review-eval.json
# 渲染端（本 clone；长流 provider=mock 的链路证据同理）
python -m kfpa.ui.eval_page --in reports/review-eval.json --format markdown --full
python -m kfpa.ui.eval_page --in reports/review-eval.json --format html     --full
```

> `review-eval-mock.*`（`provider=mock`，两组同为同源剧本，24 条：漏检 12 / 误报 12 / 一致率 0.5）
> 只作**接口链路证据**，不作为度量结论；结论以上面的正式产物为准。

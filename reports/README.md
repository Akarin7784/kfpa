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

## 接受的输入形状（宽松）

`src/kfpa/ui/eval_page.py` 归一化时按顺序匹配，四种都认：

1. `schemas.ReviewMetrics` 实例（任何带 `total/missed/agreement` 的对象也行）；
2. `{"metrics": {…}, "samples": [...]}`；
3. 扁平指标 dict（`to_jsonable(ReviewMetrics)`），可选带 `samples` / `details` / `cases`；
4. `{"groups": {"hetero": {…}, "baseline": {…}}, "samples": [...]}`。

判据按 D4 §3.4：`missed ≤ baseline_missed` 且 `false_alarms ≤ baseline_false_alarms` 且
`agreement ≥ baseline_agreement`；**没有 `baseline_*` 字段时判为「无法判定」**（不按 0 假设）。

## 本目录文件

| 文件 | 说明 |
|---|---|
| `review-eval-demo.json` | **合成示例**输入（4 条样本：1 漏检 / 1 误报 / 2 正确），用于离线自测渲染链路 |
| `review-eval-demo.md` | 由上面渲染的 Markdown 报告（示例） |
| `review-eval-demo.html` | 由上面渲染的**单文件 HTML**（内联 CSS、无 CDN/外链/脚本） |

`review-eval.json|md|html`（无 `-demo`）是正式产物，由长流 `kfpa eval review` 跑出后渲染，
不在本目录预先提交（避免把合成数据当真实度量）。

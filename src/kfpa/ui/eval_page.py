"""同源盲区度量报告渲染（P2 D4 §3.6）。

输入：``kfpa eval review --out reports/review-eval.json`` 的产物（长流 ``eval.py``）；
输出：Markdown / **单文件 HTML**（无外链、无 CDN）/ JSON，另带 Streamlit 报告页
（惰性 import，**不列入验收**）。

宽松接受的输入形状（自上而下匹配，都不抛）：

1. ``schemas.ReviewMetrics`` 实例（或任何带 ``total/missed/agreement`` 的对象）；
2. ``{"metrics": {…}, "samples": [...]}``；
3. 扁平指标 dict（``to_jsonable(ReviewMetrics)`` 的裸结构），可选带
   ``samples`` / ``details`` / ``cases``；
4. ``{"groups": {"hetero": {…}, "baseline": {…}}, "samples": [...]}`` 或
   ``{"hetero": {…}, "baseline": {…}}``。

判据口径按 D4 §3.4：``missed <= baseline_missed`` 且 ``false_alarms <= baseline_false_alarms``
且 ``agreement >= baseline_agreement``；**没有 baseline 字段时判为「无法判定」**（不假设为 0）。

headless 接口：``load_payload`` / ``normalize`` / ``render_report`` / ``table_rows`` / ``main``。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_JSON = "reports/review-eval.json"

#: 退出码（D1-5 / R-1）
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DEPS = 4
EXIT_IO = 5

_METRIC_FIELDS = (
    "corpus",
    "total",
    "missed",
    "false_alarms",
    "agreement",
    "baseline_missed",
    "baseline_false_alarms",
    "baseline_agreement",
)
_BASELINE_FIELDS = ("baseline_missed", "baseline_false_alarms", "baseline_agreement")
_SAMPLE_LIST_KEYS = ("samples", "details", "cases", "items", "rows")
_GROUP_KEYS = ("groups", "providers", "by_provider")

_STREAMLIT_HINT = (
    "[kfpa-ui] 未安装 streamlit（D1-5 依赖缺失）→ 报告页不可用；"
    "命令行渲染仍可用：`python -m kfpa.ui.eval_page --in reports/review-eval.json`。"
)


# ------------------------------------------------------------------ 基础


def _to_jsonable(value: Any) -> Any:
    """dataclass / Enum / list → 裸结构（优先用契约里的实现）。"""
    try:
        from kfpa.schemas import to_jsonable  # type: ignore[import-not-found]

        return to_jsonable(value)
    except Exception:
        if isinstance(value, dict):
            return {k: _to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_to_jsonable(v) for v in value]
        fields = getattr(value, "__dataclass_fields__", None)
        if fields:
            return {k: _to_jsonable(getattr(value, k)) for k in fields}
        return value


def contract_version() -> str:
    try:
        from kfpa.schemas import CONTRACT_VERSION

        return str(CONTRACT_VERSION)
    except Exception:
        return "unknown"


def _fmt_time(value: Any) -> str:
    """epoch 秒 → 本地时间；缺或非法 → 「未标注」。"""
    if value in (None, ""):
        return "未标注"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return str(value)


def _as_metrics(value: Any) -> dict[str, Any]:
    """只挑指标字段，缺的键留空（由 ``_fill_defaults`` 补）。"""
    raw = _to_jsonable(value)
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in _METRIC_FIELDS if k in raw}


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _int(value: Any) -> int:
    return int(_num(value))


def _fill_defaults(metrics: dict[str, Any]) -> dict[str, Any]:
    out = {
        "corpus": str(metrics.get("corpus") or ""),
        "total": _int(metrics.get("total")),
        "missed": _int(metrics.get("missed")),
        "false_alarms": _int(metrics.get("false_alarms")),
        "agreement": _num(metrics.get("agreement")),
        "baseline_missed": _int(metrics.get("baseline_missed")),
        "baseline_false_alarms": _int(metrics.get("baseline_false_alarms")),
        "baseline_agreement": _num(metrics.get("baseline_agreement")),
    }
    return out


def _flag(sample: dict[str, Any], *keys: str) -> Optional[bool]:
    """只认生产者给的显式布尔；缺或非布尔 → None。

    D4 §3.4 冻结形状：``samples`` 每条自带 ``missed`` / ``false_alarm``，渲染层**不得**自行推断，
    因此这里不做「按 human_verdict + verdict 反推」的兜底，只解析真值。
    """
    for key in keys:
        if key not in sample:
            continue
        value = sample[key]
        if isinstance(value, bool):
            return value
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
    return None


def _sample_row(sample: dict[str, Any], index: int) -> dict[str, Any]:
    human = str(sample.get("human_verdict") or sample.get("human") or "").strip().lower()
    verdict = str(
        sample.get("review_verdict") or sample.get("verdict") or sample.get("hetero_verdict") or ""
    ).strip().lower()
    baseline = str(sample.get("baseline_verdict") or sample.get("same_origin_verdict") or "").strip().lower()

    missed = _flag(sample, "missed")
    false_alarm = _flag(sample, "false_alarm", "false_positive")

    findings = sample.get("findings") or sample.get("defects") or sample.get("injected_defects") or []
    if isinstance(findings, dict):
        findings = [findings]
    if not isinstance(findings, list):
        findings = []

    return {
        "id": str(sample.get("id") or sample.get("sample_id") or f"S{index:03d}"),
        "feature": str(sample.get("feature") or ""),
        "human": human or "未标注",
        "verdict": verdict or "未判定",
        "baseline": baseline or "-",
        "missed": missed,
        "false_alarm": false_alarm,
        "flagged": missed is not None and false_alarm is not None,
        "defects": findings,
    }


def _defect_text(defects: list[Any]) -> str:
    parts = []
    for item in defects:
        if isinstance(item, dict):
            criterion = item.get("criterion") or item.get("cid") or ""
            severity = item.get("severity") or ""
            kind = item.get("kind") or item.get("type") or ""
            issue = item.get("issue") or ""
            location = item.get("location") or ""
            text = "／".join(str(x) for x in (criterion, severity, kind, issue, location) if x)
            parts.append(text or json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    return "；".join(p for p in parts if p) or "-"


def normalize(payload: Any) -> dict[str, Any]:
    """把各种输入形状归一成 ``{meta, metrics, samples, groups, has_baseline, passed, conclusion, notes}``。

    D4 §3.4 冻结形状的顶层是 ``{corpus, generated_at, provider, baseline_provider, metrics, samples}``，
    这里同时兼容扁平 dict / ``ReviewMetrics`` 实例 / ``groups`` 分组形态。
    """
    raw = _to_jsonable(payload)
    groups: dict[str, dict[str, Any]] = {}
    samples_raw: list[dict[str, Any]] = []
    has_baseline = False
    meta: dict[str, Any] = {"corpus": "", "generated_at": None, "provider": "", "baseline_provider": ""}
    notes: list[str] = []

    if isinstance(raw, dict):
        meta["corpus"] = str(raw.get("corpus") or "")
        meta["generated_at"] = raw.get("generated_at")
        meta["provider"] = str(raw.get("provider") or "")
        meta["baseline_provider"] = str(raw.get("baseline_provider") or "")

        metrics_raw = raw.get("metrics") or raw.get("review_metrics")
        if metrics_raw is None:
            metrics_raw = raw  # 扁平结构
        has_baseline = any(field in metrics_raw for field in _BASELINE_FIELDS) if isinstance(metrics_raw, dict) else False
        metrics = _fill_defaults(_as_metrics(metrics_raw))
        if not metrics["corpus"]:
            metrics["corpus"] = meta["corpus"]

        for key in _SAMPLE_LIST_KEYS:
            value = raw.get(key)
            if isinstance(value, list):
                samples_raw = [item for item in value if isinstance(item, dict)]
                break

        group_source = None
        for key in _GROUP_KEYS:
            if isinstance(raw.get(key), dict):
                group_source = raw[key]
                break
        if group_source is None:
            group_source = {name: raw[name] for name in ("hetero", "heterogeneous", "baseline") if isinstance(raw.get(name), dict)}
        if isinstance(group_source, dict):
            for name, value in group_source.items():
                item = _as_metrics(value)
                if item:
                    groups[str(name)] = _fill_defaults(item)
    else:
        metrics = _fill_defaults(_as_metrics(raw))
        has_baseline = any(hasattr(raw, field) for field in _BASELINE_FIELDS)
        meta["corpus"] = metrics["corpus"]

    samples = [_sample_row(item, index) for index, item in enumerate(samples_raw, 1)]

    unflagged = [s["id"] for s in samples if not s["flagged"]]
    if unflagged:
        notes.append(
            f"{len(unflagged)} 条样本缺 missed/false_alarm 布尔（D4 §3.4 要求生产者给出）"
            f"→ 渲染层不推断，按「未标注」处理：{'、'.join(unflagged[:8])}"
            + ("…" if len(unflagged) > 8 else "")
        )
    if not has_baseline:
        notes.append("缺 baseline_* 指标 → 结论判「无法判定」，不按 0 假设（D4 §3.4）")

    passed: Optional[bool]
    if has_baseline:
        passed = (
            metrics["missed"] <= metrics["baseline_missed"]
            and metrics["false_alarms"] <= metrics["baseline_false_alarms"]
            and metrics["agreement"] >= metrics["baseline_agreement"]
        )
        conclusion = _conclusion_text(metrics, passed)
    else:
        passed = None
        conclusion = (
            "无法判定：缺少同源 baseline 指标（baseline_missed / baseline_false_alarms / "
            "baseline_agreement）→ 不按 0 假设（D4 §3.4）"
        )

    return {
        "meta": meta,
        "metrics": metrics,
        "samples": samples,
        "groups": groups,
        "has_baseline": has_baseline,
        "passed": passed,
        "conclusion": conclusion,
        "notes": notes,
        "unflagged": unflagged,
        "missed_samples": [s for s in samples if s["missed"] is True],
        "false_alarm_samples": [s for s in samples if s["false_alarm"] is True],
    }


def _conclusion_text(metrics: dict[str, Any], passed: bool) -> str:
    verdict = "通过" if passed else "不通过"
    return (
        f"{verdict}：漏检 {metrics['missed']} ≤ 基线 {metrics['baseline_missed']}"
        f"（{'✅' if metrics['missed'] <= metrics['baseline_missed'] else '❌'}）、"
        f"误报 {metrics['false_alarms']} ≤ 基线 {metrics['baseline_false_alarms']}"
        f"（{'✅' if metrics['false_alarms'] <= metrics['baseline_false_alarms'] else '❌'}）、"
        f"一致率 {metrics['agreement']:.3f} ≥ 基线 {metrics['baseline_agreement']:.3f}"
        f"（{'✅' if metrics['agreement'] >= metrics['baseline_agreement'] else '❌'}）"
    )


# ------------------------------------------------------------------ 取数


def load_payload(path: str | Path) -> Any:
    """读度量 JSON；文件不存在 → UIDependencyError(4)，JSON 坏 → ValueError(2)。"""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"[kfpa-ui] 度量文件不存在：{target} → 先跑 `kfpa eval review --out {target}`")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"[kfpa-ui] 度量 JSON 解析失败：{exc} → 检查 {target}") from exc


def table_rows(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """headless 用的表格数据：指标对照 + 漏检/误报/全部样本。"""
    metrics = data["metrics"]
    baseline_cell = "无基线" if not data["has_baseline"] else ""
    metric_rows = [
        {
            "指标": "漏检 missed",
            "异构": metrics["missed"],
            "同源基线": metrics["baseline_missed"] if data["has_baseline"] else baseline_cell,
            "判据": "不增",
            "结果": _mark(metrics["missed"] <= metrics["baseline_missed"]) if data["has_baseline"] else "-",
        },
        {
            "指标": "误报 false_alarms",
            "异构": metrics["false_alarms"],
            "同源基线": metrics["baseline_false_alarms"] if data["has_baseline"] else baseline_cell,
            "判据": "不增",
            "结果": _mark(metrics["false_alarms"] <= metrics["baseline_false_alarms"]) if data["has_baseline"] else "-",
        },
        {
            "指标": "一致率 agreement",
            "异构": f"{metrics['agreement']:.3f}",
            "同源基线": f"{metrics['baseline_agreement']:.3f}" if data["has_baseline"] else baseline_cell,
            "判据": "不降",
            "结果": _mark(metrics["agreement"] >= metrics["baseline_agreement"]) if data["has_baseline"] else "-",
        },
        {
            "指标": "样本数 total",
            "异构": metrics["total"],
            "同源基线": metrics["total"],
            "判据": "同批种子",
            "结果": "=",
        },
    ]

    def _sample_dict(sample: dict[str, Any], kind: str) -> dict[str, Any]:
        return {
            "样本": sample["id"],
            "功能": sample["feature"],
            "人工": sample["human"],
            "异构判定": sample["verdict"],
            "同源判定": sample["baseline"],
            "问题": kind,
            "缺陷": _defect_text(sample["defects"]),
        }

    return {
        "metrics": metric_rows,
        "missed": [_sample_dict(s, "漏检") for s in data["missed_samples"]],
        "false_alarms": [_sample_dict(s, "误报") for s in data["false_alarm_samples"]],
        "samples": [_sample_dict(s, _problem_of(s)) for s in data["samples"]],
    }


def _problem_of(sample: dict[str, Any]) -> str:
    if sample["missed"] is True:
        return "漏检"
    if sample["false_alarm"] is True:
        return "误报"
    if not sample["flagged"]:
        return "未标注"
    return "—"


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


# ------------------------------------------------------------------ Markdown


def _md_table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
    return lines


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(payload: Any, *, full: bool = False) -> str:
    """渲染 Markdown 报告（两组指标对照 + 结论 + 逐样本明细）。"""
    data = normalize(payload)
    metrics = data["metrics"]
    tables = table_rows(data)

    lines = [
        "# KFPA 同源盲区度量报告（P2 / A21）",
        "",
        f"- 种子集：`{metrics['corpus'] or '未标注'}`　样本数：**{metrics['total']}**",
        f"- 异构 provider：`{data['meta']['provider'] or '未标注'}`　同源 baseline：`{data['meta']['baseline_provider'] or '未标注'}`",
        f"- 生成时间：`{_fmt_time(data['meta'].get('generated_at'))}`　契约版本：`{contract_version()}`",
        "",
        "## 指标对照（异构 REVIEWER vs 同源 baseline）",
        "",
    ]
    lines += _md_table(
        ["指标", "异构", "同源基线", "判据", "结果"],
        [[r["指标"], r["异构"], r["同源基线"], r["判据"], r["结果"]] for r in tables["metrics"]],
    )
    lines += ["", "## 结论", "", data["conclusion"], ""]
    for note in data["notes"]:
        lines.append(f"> ⚠️ {note}")
    if data["notes"]:
        lines.append("")

    if data["groups"]:
        lines += ["## 分组明细", ""]
        group_rows = [
            [name, item["total"], item["missed"], item["false_alarms"], f"{item['agreement']:.3f}"]
            for name, item in data["groups"].items()
        ]
        lines += _md_table(["组", "样本数", "漏检", "误报", "一致率"], group_rows)
        lines += [""]

    for key, title in (("missed", "漏检样本"), ("false_alarms", "误报样本")):
        lines += [f"## {title}（{len(tables[key])}）", ""]
        if tables[key]:
            lines += _md_table(
                ["样本", "功能", "人工", "异构判定", "同源判定", "缺陷"],
                [[r["样本"], r["功能"], r["人工"], r["异构判定"], r["同源判定"], r["缺陷"]] for r in tables[key]],
            )
        else:
            lines.append("（无）")
        lines.append("")

    if full:
        lines += [f"## 全部样本（{len(tables['samples'])}）", ""]
        lines += _md_table(
            ["样本", "功能", "人工", "异构判定", "同源判定", "问题", "缺陷"],
            [
                [r["样本"], r["功能"], r["人工"], r["异构判定"], r["同源判定"], r["问题"], r["缺陷"]]
                for r in tables["samples"]
            ],
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ 单文件 HTML


_HTML_CSS = """
:root{color-scheme:light dark}
body{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     margin:0;padding:2rem;line-height:1.5;background:#fafafa;color:#1f2328}
h1{font-size:1.5rem;margin:0 0 .5rem}
h2{font-size:1.1rem;margin:1.6rem 0 .5rem;border-bottom:1px solid #d0d7de;padding-bottom:.25rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:.9rem}
th,td{border:1px solid #d0d7de;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:#f3f4f6}
.pass{color:#0a7d32;font-weight:600}
.fail{color:#b42318;font-weight:600}
.unknown{color:#9a6700;font-weight:600}
.note{color:#9a6700;font-size:.88rem;margin:.2rem 0}
.meta{color:#57606a;font-size:.85rem}
code{background:#f3f4f6;padding:0 .2rem;border-radius:3px}
"""


def render_html(payload: Any, *, full: bool = False) -> str:
    """渲染**单文件 HTML**：内联 CSS、无 CDN、无外链、无可执行脚本。"""
    data = normalize(payload)
    metrics = data["metrics"]
    tables = table_rows(data)

    passed = data["passed"]
    cls, label = ("pass", "通过") if passed is True else (("fail", "不通过") if passed is False else ("unknown", "无法判定"))

    def _table(header: list[str], rows: list[dict[str, Any]]) -> str:
        head = "".join(f"<th>{html.escape(h)}</th>" for h in header)
        body = []
        for row in rows:
            cells = "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row.values())
            body.append(f"<tr>{cells}</tr>")
        body_text = "".join(body) if body else f'<tr><td colspan="{len(header)}">（无）</td></tr>'
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body_text}</tbody></table>"

    parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        "<title>KFPA 同源盲区度量报告</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head><body>",
        "<h1>KFPA 同源盲区度量报告（P2 / A21）</h1>",
        f'<p class="meta">种子集 <code>{html.escape(metrics["corpus"] or "未标注")}</code>'
        f"　样本数 <b>{metrics['total']}</b>"
        f"　异构 <code>{html.escape(data['meta']['provider'] or '未标注')}</code>"
        f"　同源 <code>{html.escape(data['meta']['baseline_provider'] or '未标注')}</code>"
        f"　生成 {html.escape(_fmt_time(data['meta'].get('generated_at')))}"
        f"　契约 <code>{html.escape(contract_version())}</code></p>",
        "<h2>指标对照（异构 REVIEWER vs 同源 baseline）</h2>",
        _table(["指标", "异构", "同源基线", "判据", "结果"], tables["metrics"]),
        "<h2>结论</h2>",
        f'<p class="{cls}">{html.escape(data["conclusion"])}</p>',
    ]
    for note in data["notes"]:
        parts.append(f'<p class="note">⚠️ {html.escape(note)}</p>')

    if data["groups"]:
        parts.append("<h2>分组明细</h2>")
        parts.append(
            _table(
                ["组", "样本数", "漏检", "误报", "一致率"],
                [
                    {"组": name, "样本数": item["total"], "漏检": item["missed"],
                     "误报": item["false_alarms"], "一致率": f"{item['agreement']:.3f}"}
                    for name, item in data["groups"].items()
                ],
            )
        )

    for key, title in (("missed", "漏检样本"), ("false_alarms", "误报样本")):
        parts.append(f"<h2>{title}（{len(tables[key])}）</h2>")
        parts.append(
            _table(
                ["样本", "功能", "人工", "异构判定", "同源判定", "缺陷"],
                [
                    {k: r[k] for k in ("样本", "功能", "人工", "异构判定", "同源判定", "缺陷")}
                    for r in tables[key]
                ],
            )
        )

    if full:
        parts.append(f"<h2>全部样本（{len(tables['samples'])}）</h2>")
        parts.append(
            _table(
                ["样本", "功能", "人工", "异构判定", "同源判定", "问题", "缺陷"],
                tables["samples"],
            )
        )

    parts.append(f'<p class="meta">{html.escape(label)}　生成时间 {html.escape(time.strftime("%Y-%m-%d %H:%M:%S"))}</p>')
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def render_json(payload: Any) -> str:
    """把归一化结果回吐成 JSON（便于下游再加工）。"""
    data = normalize(payload)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def render_report(payload: Any, fmt: str = "markdown", *, full: bool = False) -> str:
    """统一渲染入口：``markdown`` / ``md`` / ``html`` / ``json``。"""
    key = (fmt or "markdown").strip().lower()
    if key in ("markdown", "md"):
        return render_markdown(payload, full=full)
    if key in ("html", "htm"):
        return render_html(payload, full=full)
    if key == "json":
        return render_json(payload)
    raise ValueError(f"[kfpa-ui] 未知报告格式：{fmt}（可用：markdown/md/html/json）")


# ------------------------------------------------------------------ 页面（不列入验收）


def page(st, payload: Any, *, full: bool = True) -> dict[str, Any]:
    """Streamlit 报告页；返回归一化数据（headless 可断言）。"""
    data = normalize(payload)
    st.set_page_config(page_title="KFPA 同源盲区度量", layout="wide")
    st.title("KFPA 同源盲区度量报告")
    st.caption("P2 / A21：异构 REVIEWER vs 同源 baseline，判据＝不漏检、不增误报、不降一致率（D4 §3.4）。")
    st.caption(
        f"种子集 `{data['meta']['corpus'] or '未标注'}`　异构 `{data['meta']['provider'] or '未标注'}`"
        f"　同源 `{data['meta']['baseline_provider'] or '未标注'}`"
        f"　生成 {_fmt_time(data['meta'].get('generated_at'))}"
    )
    for note in data["notes"]:
        st.warning(note)

    metrics = data["metrics"]
    left, middle, right = st.columns(3)
    left.metric("漏检 missed", metrics["missed"], delta=metrics["missed"] - metrics["baseline_missed"] if data["has_baseline"] else None)
    middle.metric("误报 false_alarms", metrics["false_alarms"], delta=metrics["false_alarms"] - metrics["baseline_false_alarms"] if data["has_baseline"] else None)
    right.metric("一致率 agreement", f"{metrics['agreement']:.3f}")

    if data["passed"] is True:
        st.success(data["conclusion"])
    elif data["passed"] is False:
        st.error(data["conclusion"])
    else:
        st.warning(data["conclusion"])

    tables = table_rows(data)
    st.subheader("指标对照")
    st.dataframe(tables["metrics"], width="stretch")

    st.subheader(f"漏检样本（{len(tables['missed'])}）")
    st.dataframe(tables["missed"] or [{"提示": "无漏检样本"}], width="stretch")
    st.subheader(f"误报样本（{len(tables['false_alarms'])}）")
    st.dataframe(tables["false_alarms"] or [{"提示": "无误报样本"}], width="stretch")

    if full:
        with st.expander(f"全部样本（{len(tables['samples'])}）", expanded=False):
            st.dataframe(tables["samples"], width="stretch")

    with st.expander("原始 JSON", expanded=False):
        st.code(render_json(payload), language="json")
    return data


def render_headless(path: str | Path = DEFAULT_JSON, *, full: bool = True) -> dict[str, Any]:
    """用 AppTest 真渲染一次报告页（不进浏览器），返回归一化数据。"""
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError as exc:
        raise RuntimeError(_STREAMLIT_HINT) from exc

    os.environ["KFPA_EVAL_JSON"] = str(path)
    # AppTest 下脚本以 __main__ 执行；末尾 raise SystemExit 会让 AppTest 取不到
    # 「脚本无错」内部键（KeyError），用这个标记切到页面渲染分支（同 streamlit_app）
    previous = os.environ.get("KFPA_UI_APPTEST")
    os.environ["KFPA_UI_APPTEST"] = "1"
    try:
        app = AppTest.from_file(str(Path(__file__).resolve()), default_timeout=60).run()
    finally:
        if previous is None:
            os.environ.pop("KFPA_UI_APPTEST", None)
        else:
            os.environ["KFPA_UI_APPTEST"] = previous

    if app.exception:
        messages = []
        for item in app.exception:
            value = getattr(item, "value", item)
            messages.append(f"{type(value).__name__}: {value}")
        raise RuntimeError("报告页渲染异常：" + "；".join(messages))
    data = app.session_state.get("kfpa_eval_data")
    if not data:
        raise RuntimeError("[kfpa-ui] 报告页没有产出数据（检查 page() 是否被调用）")
    return dict(data)


# ------------------------------------------------------------------ 入口


def _default_out(input_path: Path, fmt: str) -> Path:
    suffix = {"markdown": ".md", "md": ".md", "html": ".html", "json": ".json"}[fmt]
    return input_path.with_suffix(suffix)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kfpa-ui-eval-page",
        description="同源盲区度量报告渲染（Markdown / 单文件 HTML / JSON）",
    )
    parser.add_argument("--in", dest="input_path", default=None, metavar="PATH",
                        help=f"度量 JSON（默认 {DEFAULT_JSON}）")
    parser.add_argument("--out", dest="output_path", default=None, metavar="PATH", help="输出文件（默认与输入同名的 .md/.html/.json）")
    parser.add_argument("--format", default="markdown", choices=["markdown", "md", "html", "json"], help="报告格式")
    parser.add_argument("--full", action="store_true", help="附带全部样本明细")
    parser.add_argument("--stdout", action="store_true", help="不写文件，直接打到 stdout")
    parser.add_argument("--page", action="store_true", help="以 Streamlit 页面渲染（需要 streamlit）")
    args, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    input_path = Path(args.input_path or os.environ.get("KFPA_EVAL_JSON") or DEFAULT_JSON)

    if args.page:
        try:
            import streamlit as st
        except ImportError:
            print(_STREAMLIT_HINT, file=sys.stderr)
            return EXIT_DEPS
        try:
            page(st, load_payload(input_path))
        except FileNotFoundError as exc:
            st.error(str(exc))
            return EXIT_DEPS
        except ValueError as exc:
            st.error(str(exc))
            return EXIT_USAGE
        return EXIT_OK

    try:
        payload = load_payload(input_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_DEPS
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    text = render_report(payload, args.format, full=args.full)
    if args.stdout:
        sys.stdout.write(text)
        return EXIT_OK

    out_path = Path(args.output_path) if args.output_path else _default_out(input_path, args.format)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"[kfpa-ui] 报告写入失败：{exc} → 检查 {out_path} 的目录与权限", file=sys.stderr)
        return EXIT_IO
    print(f"[kfpa-ui] 报告已写入 {out_path}（{args.format}，{len(text)} 字节）")
    return EXIT_OK


if __name__ == "__main__":
    if not os.environ.get("KFPA_UI_APPTEST"):
        raise SystemExit(main())
    # AppTest 下脚本以 __main__ 执行：渲染报告页并把数据放进 session_state
    import streamlit as _st  # noqa: E402

    _rc = 0
    try:
        _data = page(_st, load_payload(os.environ.get("KFPA_EVAL_JSON") or DEFAULT_JSON))
        _st.session_state["kfpa_eval_data"] = _data
    except FileNotFoundError:
        # 文件不存在时给可读占位，不让 AppTest 崩
        _st.session_state["kfpa_eval_data"] = normalize({"metrics": {}, "samples": []})

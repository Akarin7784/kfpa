"""知识沉淀**隔离区 / 抽检页**（P3 / D5 §3.3 · §3.6）—— 渲染层可 headless 调用。

D5 §3.3 的闸门全量（门槛 / 隔离区 / 转正 / 抽检 / 丢弃）由 ``kfpa.knowledge`` 实现；
本页只做**取数与展示**，把三张面孔摊给操作者：

1. **隔离区**：``QUARANTINE`` —— 过门槛但还没被后续轮次复用（同轮复用不算，D5 §3.3.2）。
2. **转正 / 丢弃清单**：``PROMOTED`` / ``DROPPED``；被丢弃的会同时进回避模式清单（负知识）。
3. **抽检队列 + 留痕**：``review_sample`` 的命中清单与 ``KnowledgeSample`` 留痕
   （``sampled_by`` / ``rate`` / ``passed``）。

取数只走 D5 §3.3/.4 冻结的 CLI：``kfpa knowledge list --json``（P1 起就有，键为
``candidates`` + ``sample``）；实现方若再加 ``samples`` / ``report`` 键，本页**多认不猜**。
本页**不写库**（转正由长流的 ``promote`` / ``verdict`` 命令做），也不 import streamlit 才能用。
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import fastlane_page as _fp

__all__ = [
    "CANDIDATE_FIELDS",
    "DEFAULT_DB",
    "DEFAULT_OUT_DIR",
    "KNOWLEDGE_MISSING_HINT",
    "SAMPLE_FIELDS",
    "STATUS_LABELS",
    "bucket",
    "load_overview",
    "main",
    "normalize_candidate",
    "normalize_requests_rows",
    "normalize_sample",
    "render",
    "render_headless",
    "render_html",
    "render_markdown",
    "write_reports",
]

DEFAULT_DB = "kfpa.db"
DEFAULT_OUT_DIR = "reports"

KNOWLEDGE_MISSING_HINT = (
    "[kfpa-ui] `kfpa knowledge list --json` 不可用（模块没挂或库没建）→ 本页不做展示；"
    "先跑 `python -m kfpa init --db <库>` 与 `p3/knowledge` 的合入。"
)

#: 契约里的 KnowledgeCandidate 字段（schemas.py §P1）
CANDIDATE_FIELDS = ("cid", "round_no", "kind", "content", "confidence", "status", "reused_by", "created_at")
#: 契约 1.4 的 KnowledgeSample 字段（抽检留痕）
SAMPLE_FIELDS = ("cid", "sampled_at", "sampled_by", "rate", "passed")

STATUS_LABELS = {
    "quarantine": "隔离区（待后续轮次复用转正）",
    "promoted": "已转正",
    "dropped": "已丢弃",
}

_KIND_LABELS = {"success": "正知识", "avoid": "负知识（回避模式）"}
_NESTED_CANDIDATE_KEYS = ("candidates", "items", "rows", "data")
_NESTED_SAMPLE_KEYS = ("samples", "sample", "sampled", "review_sample")


# ------------------------------------------------------------------ 归一化（纯）


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def _as_int_list(value: Any) -> list[int]:
    out: list[int] = []
    for item in _as_str_list(value):
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def normalize_candidate(raw: Any) -> dict[str, Any]:
    """把一行候选按契约字段归一；认不出的字段丢弃（不猜别名）。"""
    if isinstance(raw, dict):
        data = dict(raw)
    elif hasattr(raw, "__dataclass_fields__"):
        from dataclasses import asdict

        data = asdict(raw)
    else:
        data = {name: getattr(raw, name) for name in CANDIDATE_FIELDS if hasattr(raw, name)}

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    status = _enum_value(data.get("status") or "").strip().lower()
    kind = str(data.get("kind") or "")
    row = {
        "cid": str(data.get("cid") or ""),
        "round_no": int(data.get("round_no") or 0),
        "kind": kind,
        "kind_label": _KIND_LABELS.get(kind, kind or "-"),
        "content": str(data.get("content") or ""),
        "confidence": confidence,
        "status": status,
        "status_label": STATUS_LABELS.get(status, f"未知状态（{status or '空'}）"),
        "reused_by": _as_int_list(data.get("reused_by")),
        "created_at": float(data.get("created_at") or 0.0),
    }
    return row


def normalize_sample(raw: Any) -> dict[str, Any]:
    """抽检留痕 / 抽检命中行（两种来源都认：``KnowledgeSample`` 或 ``review_sample`` 的 dict）。"""
    if isinstance(raw, dict):
        data = dict(raw)
    elif hasattr(raw, "__dataclass_fields__"):
        from dataclasses import asdict

        data = asdict(raw)
    else:
        data = {name: getattr(raw, name) for name in SAMPLE_FIELDS if hasattr(raw, name)}

    def _num(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    rate = data.get("rate", data.get("sample_rate"))
    return {
        "cid": str(data.get("cid") or ""),
        "sampled_at": _num(data.get("sampled_at")),
        "sampled_by": str(data.get("sampled_by") or ""),
        "rate": _num(rate),
        "passed": None if data.get("passed") is None else bool(data.get("passed")),
    }


def normalize_requests_rows(payload: Any, keys: Iterable[str]) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in keys:
            nested = payload.get(key)
            if isinstance(nested, list):
                return [normalize_candidate(item) for item in nested if item is not None]
        return []
    if isinstance(payload, (list, tuple)):
        return [normalize_candidate(item) for item in payload if item is not None]
    return []


def bucket(candidates: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按状态分桶；正/负知识也分列（负知识不进正区，D5 §3.3）。"""
    out: dict[str, list[dict[str, Any]]] = {"quarantine": [], "promoted": [], "dropped": [], "unknown": []}
    for row in candidates:
        key = row.get("status") if row.get("status") in out else "unknown"
        out[key].append(row)
    return out


# ------------------------------------------------------------------ 取数


def load_overview(
    *,
    db: Optional[str] = DEFAULT_DB,
    runner: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """``kfpa knowledge list --json`` → ``{candidates, samples, hint, rc}``（拿不到不编造）。"""
    argv = ["knowledge", "list", "--json"]
    if db:
        argv += ["--db", str(db)]
    rc, out, err = _fp.run_cli(runner, argv, db=db)
    if rc != 0:
        detail = (err or out).strip()[:400]
        return {
            "candidates": [],
            "samples": [],
            "hint": f"[kfpa-ui] `kfpa knowledge list` 退出码 {rc}：{detail}",
            "rc": rc,
        }
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return {
            "candidates": [],
            "samples": [],
            "hint": "[kfpa-ui] `knowledge list --json` 输出不是 JSON → 核对 D3-3.5/D5 §3.3 的稳定键",
            "rc": rc,
        }

    candidates = normalize_requests_rows(payload, _NESTED_CANDIDATE_KEYS)
    samples: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in _NESTED_SAMPLE_KEYS:
            nested = payload.get(key)
            if isinstance(nested, list):
                samples = [normalize_sample(item) for item in nested if item is not None]
                break
    return {"candidates": candidates, "samples": samples, "hint": "", "rc": rc, "raw_keys": sorted(payload) if isinstance(payload, dict) else []}


# ------------------------------------------------------------------ 展示


def _kind_badge(row: dict[str, Any]) -> str:
    return f"{row['kind_label']}"


def table_rows(overview: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """三个表格行 + 抽检队列（渲染与 headless 共用同一形状）。"""
    rows = overview.get("candidates") or []
    buckets = bucket(rows)

    def _cand(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "票号": item["cid"],
            "轮次": item["round_no"],
            "类型": _kind_badge(item),
            "置信度": f"{item['confidence']:.2f}",
            "状态": item["status_label"],
            "成功复用轮次": "、".join(str(r) for r in item["reused_by"]) or "-",
            "内容": item["content"][:60] + ("…" if len(item["content"]) > 60 else ""),
        }

    return {
        "quarantine": [_cand(item) for item in buckets["quarantine"]],
        "promoted": [_cand(item) for item in buckets["promoted"]],
        "dropped": [_cand(item) for item in buckets["dropped"]],
        "unknown": [_cand(item) for item in buckets["unknown"]],
        "samples": [
            {
                "票号": item["cid"],
                "抽样率": f"{item['rate']:.4f}",
                "抽检人": item["sampled_by"] or "-",
                "时间": _fp._fmt_time(item["sampled_at"]),
                "结论": "-" if item["passed"] is None else ("通过" if item["passed"] else "不通过（→ 丢弃 + 回避模式）"),
            }
            for item in overview.get("samples") or []
        ],
    }


def render(st, overview: dict[str, Any], *, key_prefix: str = "knowledge") -> dict[str, Any]:
    """Streamlit 段落：隔离区 / 转正 / 丢弃 / 抽检队列；返回统计（headless 可断言）。"""
    st.subheader("知识沉淀闸门（隔离区 / 抽检）")
    st.caption("口径：D5 §3.3 —— 低于门槛直接丢弃；隔离区只被**后续轮次**复用才转正；抽检不过 → 丢弃 + 回避模式。")

    hint = overview.get("hint") or ""
    if hint:
        st.warning(hint)
        return {"rows": 0, "hint": hint}

    tables = table_rows(overview)
    counts = {key: len(tables[key]) for key in ("quarantine", "promoted", "dropped", "unknown")}
    st.info(
        f"隔离区 {counts['quarantine']} · 已转正 {counts['promoted']} · 已丢弃 {counts['dropped']}"
        + (f" · 状态未知 {counts['unknown']}" if counts["unknown"] else "")
    )

    st.markdown("**隔离区（等后续轮次复用）**")
    if tables["quarantine"]:
        st.dataframe(tables["quarantine"], width="stretch")
    else:
        st.caption("（空）")

    st.markdown("**已转正**")
    if tables["promoted"]:
        st.dataframe(tables["promoted"], width="stretch")
    else:
        st.caption("（空）")

    st.markdown("**已丢弃（含抽检不过 → 同时进回避模式清单）**")
    if tables["dropped"]:
        st.dataframe(tables["dropped"], width="stretch")
    else:
        st.caption("（空）")

    st.markdown("**抽检队列 / 留痕**")
    if tables["samples"]:
        st.dataframe(tables["samples"], width="stretch")
        st.caption("抽检结论由 `kfpa knowledge verdict` 落库；本页只读不改（D5 §3.3.3）。")
    else:
        st.caption("（本轮没有抽中候选）")

    if counts["unknown"]:
        st.warning("有候选状态认不出（不是 quarantine/promoted/dropped），已单列展示，请核对 `kfpa.knowledge` 的状态口径。")

    return {"rows": len(overview.get("candidates") or []), "counts": counts, "hint": ""}


# ------------------------------------------------------------------ 报告


def _counts(overview: dict[str, Any]) -> dict[str, int]:
    return {key: len(value) for key, value in bucket(overview.get("candidates") or []).items()}


def render_markdown(overview: dict[str, Any], *, generated_at: Optional[float] = None) -> str:
    stamp = time.time() if generated_at is None else generated_at
    tables = table_rows(overview)
    counts = _counts(overview)
    lines = [
        "# 知识沉淀闸门报告",
        "",
        f"- 生成时间：{_fp._fmt_time(stamp)}",
        f"- 候选总数：{len(overview.get('candidates') or [])}",
        f"- 隔离区 {counts['quarantine']} · 已转正 {counts['promoted']} · 已丢弃 {counts['dropped']}"
        + (f" · 状态未知 {counts['unknown']}" if counts["unknown"] else ""),
        "- 口径：D5 §3.3（门槛 / 隔离区 / 后续轮次才转正 / 抽检不过 → 丢弃 + 回避模式）",
        "",
        "## 隔离区",
        "",
    ]
    lines += _md_table(tables["quarantine"])
    lines += ["", "## 已转正", ""] + _md_table(tables["promoted"])
    lines += ["", "## 已丢弃", ""] + _md_table(tables["dropped"])
    lines += ["", "## 抽检队列 / 留痕", ""] + _md_table(tables["samples"])
    lines += ["", "> 本报告由 `kfpa.ui.knowledge_page` 渲染；落库动作由 `kfpa.knowledge` 的命令执行。", ""]
    return "\n".join(lines)


def _md_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["（空）"]
    header = list(rows[0])
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        out.append("| " + " | ".join(_md_cell(row.get(name, "")) for name in header) + " |")
    return out


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_html(overview: dict[str, Any], *, generated_at: Optional[float] = None) -> str:
    """单文件 HTML（内联 CSS、无 CDN、无外链、无脚本；可离线打印，D5 §3.6）。"""
    stamp = time.time() if generated_at is None else generated_at
    tables = table_rows(overview)
    counts = _counts(overview)
    sections = (
        ("隔离区（等后续轮次复用）", tables["quarantine"]),
        ("已转正", tables["promoted"]),
        ("已丢弃（含抽检不过 → 回避模式）", tables["dropped"]),
        ("抽检队列 / 留痕", tables["samples"]),
    )
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>知识沉淀闸门报告</title>",
        f"<style>{_fp._HTML_CSS}</style>",
        "</head>",
        "<body>",
        "<h1>知识沉淀闸门报告</h1>",
        '<p class="meta">'
        f"生成时间：{html.escape(_fp._fmt_time(stamp))} · 候选总数：{len(overview.get('candidates') or [])} · "
        f"隔离区 {counts['quarantine']} · 已转正 {counts['promoted']} · 已丢弃 {counts['dropped']}"
        "</p>",
    ]
    for heading, rows in sections:
        parts.append(f"<h2>{html.escape(heading)}</h2>")
        if not rows:
            parts.append('<p class="meta">（空）</p>')
            continue
        header = list(rows[0])
        parts.append("<table><thead><tr>" + "".join(f"<th>{html.escape(str(name))}</th>" for name in header) + "</tr></thead><tbody>")
        for row in rows:
            parts.append("<tr>" + "".join(f"<td>{html.escape(str(row.get(name, '')))}</td>" for name in header) + "</tr>")
        parts.append("</tbody></table>")
    parts += [
        '<p class="note">口径：D5 §3.3 —— 低于置信度门槛直接丢弃；隔离区只被后续轮次复用才转正（同轮不算）；'
        "抽检不过 → 丢弃并写入回避模式清单。本文件单文件离线可打印，无外部依赖。</p>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(parts)


def write_reports(
    overview: dict[str, Any],
    *,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    formats: Iterable[str] = ("md", "html"),
) -> dict[str, Any]:
    """落 ``reports/knowledge-report.{md,html}``（D5 §3.6）。失败不抛，回报 errors。"""
    directory = Path(out_dir)
    files: list[str] = []
    errors: list[str] = []
    for fmt in formats:
        fmt = str(fmt).lower()
        if fmt not in ("md", "html"):
            errors.append(f"不认识的格式：{fmt}")
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"knowledge-report.{fmt}"
            text = render_markdown(overview) if fmt == "md" else render_html(overview)
            path.write_text(text, encoding="utf-8")
            files.append(str(path))
        except OSError as exc:
            errors.append(f"{fmt} 写入失败：{exc} → 检查目录权限")
    return {"files": files, "errors": errors}


# ------------------------------------------------------------------ headless + 入口


def render_headless(
    *,
    db: Optional[str] = DEFAULT_DB,
    overview: Optional[dict[str, Any]] = None,
    runner: Optional[Callable[..., Any]] = None,
    out_dir: Optional[str] = None,
) -> dict[str, Any]:
    data = overview if overview is not None else load_overview(db=db, runner=runner)
    summary = {
        "rows": len(data.get("candidates") or []),
        "counts": _counts(data),
        "samples": len(data.get("samples") or []),
        "hint": data.get("hint", ""),
        "reports": [],
    }
    if out_dir and not data.get("hint"):
        written = write_reports(data, out_dir=out_dir)
        summary["reports"] = written["files"]
        if written["errors"]:
            summary["report_errors"] = written["errors"]
    return summary


def main(argv: Optional[list[str]] = None, *, runner: Optional[Callable[..., Any]] = None) -> int:
    """``python -m kfpa.ui.knowledge_page --db … --out-dir reports``。"""
    import argparse

    parser = argparse.ArgumentParser(prog="kfpa-ui-knowledge", description="知识沉淀闸门展示与报告渲染（P3）")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--format", choices=("md", "html", "both", "none"), default="both")
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    data = load_overview(db=args.db, runner=runner)
    if data.get("hint") and not data.get("candidates"):
        print(data["hint"])
        return 4 if "退出码 2" in str(data["hint"]) or "knowledge" in str(data["hint"]) else 5

    files: list[str] = []
    if args.format != "none":
        formats = ("md", "html") if args.format == "both" else (args.format,)
        written = write_reports(data, out_dir=args.out_dir, formats=formats)
        files = written["files"]
        for error in written["errors"]:
            print(error)

    if args.json:
        print(json.dumps({"counts": _counts(data), "files": files, "samples": len(data.get("samples") or [])}, ensure_ascii=False))
    elif args.stdout:
        print(render_markdown(data))
    else:
        print(f"候选 {len(data.get('candidates') or [])} 条，抽检 {len(data.get('samples') or [])} 条；报告：{'、'.join(files) or '（未写）'}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 模块入口
    raise SystemExit(main())

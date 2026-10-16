"""快车道**承认页**（P3 / D5 §3.2 · §3.6）—— 真入库，不再是预览。

R-23 的「P1 只做预览、P3 才落库」在 P3 兑现：本页承认会**真的写** ``fastlane_requests``，
并把 ``batch_id`` / ``decided_by`` / ``decided_at`` 摊在页面上（D5 §3.6）。
P1 的预览页仍在 ``kfpa/ui/fastlane.py``（判据侧预演，明确标注不写库）。

三条硬口径（不许自己发明）：

1. **一键都不许凭空写库**：唯一写入口是 D5 §3.2 冻结的 CLI ——
   ``kfpa fastlane batch-ack --file <acks.yaml> --by <name>``；
   写完后本页**回读** ``kfpa fastlane list --json`` 确认状态真的翻了、``decided_by`` 是我们传的人，
   回读不认就报「写入未确认」，**不谎报成功**。
2. **``ESCALATED`` / ``REJECTED`` 不许被 ack 翻白**（D5 §3.2）：这些行在界面上根本不进勾选池，
   即使被硬塞进来也在 ``plan_ack()`` 里被拒并指名原因（要翻必须新开请求）。
3. **防误点**：本批勾选的条数与人手填的确认条数**必须一致**才放行（D5 §3.6），
   这条判定是纯函数 ``plan_ack()``，可 headless 断言。

数据形状一律取 ``kfpa.schemas.FastlaneRequest``（契约 1.4，Chief 冻结）；
本模块不 import streamlit 也能用（``render`` 只在使用时才要 st），不直接开数据库连接。
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "ACK_BANNER",
    "ACKABLE_STATUSES",
    "BLOCKED_STATUSES",
    "DEFAULT_DB",
    "DEFAULT_OUT_DIR",
    "FASTLANE_MISSING_HINT",
    "REQUEST_FIELDS",
    "STATUS_LABELS",
    "batch_ack",
    "build_acks_yaml",
    "confirm_label",
    "group_by_batch",
    "list_requests",
    "main",
    "normalize_request",
    "normalize_requests",
    "plan_ack",
    "render",
    "render_headless",
    "render_html",
    "render_json",
    "render_markdown",
    "run_cli",
    "status_label",
    "write_reports",
]

DEFAULT_DB = "kfpa.db"
DEFAULT_OUT_DIR = "reports"

#: D5 §3.2：只有这两个态可以被承认（``approved`` 允许幂等重放，实现方返回原记录）
ACKABLE_STATUSES = ("pending", "approved")
#: D5 §3.2：这两个态不许被 ack 翻成 approved（要翻必须新开请求）
BLOCKED_STATUSES = ("escalated", "rejected")

#: 契约 1.4 ``FastlaneRequest`` 的字段名（D5 §3.1，Chief 冻结）—— 归一化的唯一依据
REQUEST_FIELDS = (
    "request_id",
    "round_no",
    "summary",
    "diff_files",
    "new_interface",
    "new_data_model",
    "touched_modules",
    "status",
    "batch_id",
    "decided_by",
    "decided_at",
    "note",
)

STATUS_LABELS = {
    "pending": "待承认",
    "approved": "已承认（可直跑）",
    "rejected": "人工拒绝",
    "escalated": "已升级回完整设计回合",
}

ACK_BANNER = (
    "P3 起本页是**真承认**：点确认会写 `fastlane_requests`（D5 §3.2），"
    "并留下 `batch_id` / `decided_by` / `decided_at`。"
)

FASTLANE_MISSING_HINT = (
    "[kfpa-ui] `kfpa fastlane` 子命令未挂载（P3 的 `kfpa.fastlane` 还没进 cli.py 插件清单）→ "
    "本页不做承认，只提示；等 p3/store 合入后重跑即可。"
)

#: 承认时回读 list 的最大等待（子进程总超时）
DEFAULT_TIMEOUT = 60

_CMD_SEP = " · "


# ------------------------------------------------------------------ 子进程 seam


def _child_env(db: Optional[str]) -> dict[str, str]:
    """给 `python -m kfpa` 准备环境：保证源码树可 import + 显式 ``--db`` 不被 env 抢走。"""
    env = dict(os.environ)
    if db is not None:
        # R-25：给了显式 --db 就让它权威（否则 KFPA_DB_URL 会在「--db 是默认值」时抢走）
        env.pop("KFPA_DB_URL", None)
    try:
        import kfpa  # noqa: PLC0415 - 惰性：包自身位置

        root = str(Path(kfpa.__file__).parent.parent)
        current = env.get("PYTHONPATH", "")
        parts = [p for p in current.split(os.pathsep) if p]
        if root not in parts:
            env["PYTHONPATH"] = os.pathsep.join([root, *parts])
    except Exception:
        pass
    return env


def _default_runner(argv: list[str], *, db: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
    """默认执行 `python -m kfpa <argv…>`；返回 ``subprocess.CompletedProcess``。"""
    cmd = [sys.executable, "-m", "kfpa", *argv]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(db),
        encoding="utf-8",
        errors="replace",
    )


def run_cli(runner: Optional[Callable[..., Any]], argv: list[str], *, db: Optional[str]) -> tuple[int, str, str]:
    """跑一次 CLI，返回 ``(rc, stdout, stderr)``；runner 抛异常 → rc=-1 + 中文原因。

    ``runner`` 是唯一 seam：默认走 ``python -m kfpa``，测试可注入假 runner（不发子进程）。
    """
    try:
        if runner is None:
            proc = _default_runner(argv, db=db)
        else:
            proc = runner(argv, db=db)
    except Exception as exc:  # 起不了子进程也要给人话
        return -1, "", f"{type(exc).__name__}: {exc} → 检查 PYTHONPATH 与 `python -m kfpa` 是否可用"
    rc = int(getattr(proc, "returncode", -1))
    out = getattr(proc, "stdout", "") or ""
    err = getattr(proc, "stderr", "") or ""
    return rc, out, err


# ------------------------------------------------------------------ 归一化（纯）


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def status_value(raw: Any) -> str:
    """状态归一成小写字符串；认不出的原样返回（不猜、不默认 pending）。"""
    return _enum_value(raw).strip().lower()


def status_label(raw: Any) -> str:
    key = status_value(raw)
    return STATUS_LABELS.get(key, f"未知状态（{key or '空'}）")


def _as_list_of_str(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def normalize_request(raw: Any) -> dict[str, Any]:
    """把一行（dict / dataclass / JSON 片段）按契约 1.4 的字段名归一。

    认不出的字段**丢弃**（不猜别名），缺的字段按冻结默认值补；``status`` 认不出时保留原值，
    由 ``plan_ack()`` 判为不可承认 —— 不替实现方猜语义。
    """
    if isinstance(raw, dict):
        data = dict(raw)
    elif hasattr(raw, "__dataclass_fields__"):
        from dataclasses import asdict

        data = asdict(raw)
    else:
        data = {name: getattr(raw, name) for name in REQUEST_FIELDS if hasattr(raw, name)}

    row: dict[str, Any] = {
        "request_id": str(data.get("request_id") or ""),
        "round_no": int(data.get("round_no") or 0),
        "summary": str(data.get("summary") or ""),
        "diff_files": _as_list_of_str(data.get("diff_files")),
        "new_interface": bool(data.get("new_interface")),
        "new_data_model": bool(data.get("new_data_model")),
        "touched_modules": _as_list_of_str(data.get("touched_modules")),
        "status": status_value(data.get("status")),
        "batch_id": str(data.get("batch_id") or ""),
        "decided_by": str(data.get("decided_by") or ""),
        "decided_at": float(data.get("decided_at") or 0.0),
        "note": str(data.get("note") or ""),
    }
    row["status_label"] = status_label(row["status"])
    row["ackable"] = row["status"] in ACKABLE_STATUSES
    row["blocked"] = row["status"] in BLOCKED_STATUSES
    return row


_NESTED_KEYS = ("requests", "items", "rows", "data", "records")


def normalize_requests(payload: Any) -> list[dict[str, Any]]:
    """把 ``list --json`` 的各种形状摊平成行：``[...]`` / ``{"requests": [...]}`` 等。"""
    if payload is None:
        return []
    if isinstance(payload, (str, bytes)):
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(payload, dict):
        for key in _NESTED_KEYS:
            nested = payload.get(key)
            if isinstance(nested, list):
                return [normalize_request(item) for item in nested if item is not None]
        # 单条请求的裸对象
        if payload.get("request_id"):
            return [normalize_request(payload)]
        return []
    if isinstance(payload, (list, tuple)):
        return [normalize_request(item) for item in payload if item is not None]
    return []


def group_by_batch(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按 ``batch_id`` 分组；未承认过的（空 batch_id）归到 ``""``。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("batch_id") or ""), []).append(row)
    return groups


def _fmt_time(value: Any) -> str:
    try:
        stamp = float(value or 0.0)
    except (TypeError, ValueError):
        return "-"
    if stamp <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


# ------------------------------------------------------------------ 取数（只走冻结 CLI）


def list_requests(
    *,
    db: Optional[str] = DEFAULT_DB,
    status: Optional[str] = None,
    runner: Optional[Callable[..., Any]] = None,
) -> tuple[list[dict[str, Any]], str]:
    """读承认台账：``kfpa fastlane list [--status S] --json``（D5 §3.2 冻结命令）。

    返回 ``(行, 提示)``；子命令没挂 / 输出不是 JSON → ``([], 中文提示)``，**不猜、不编造行**。
    """
    argv = ["fastlane", "list", "--json"]
    if status:
        argv += ["--status", str(status)]
    if db:
        argv += ["--db", str(db)]
    rc, out, err = run_cli(runner, argv, db=db)
    if rc == 2:
        return [], FASTLANE_MISSING_HINT
    if rc != 0:
        return [], f"[kfpa-ui] `kfpa fastlane list` 退出码 {rc}：{(err or out).strip()[:400]}"
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return [], "[kfpa-ui] `fastlane list --json` 输出不是 JSON → 核对 D5 §3.2 的稳定列"
    return normalize_requests(payload), ""


# ------------------------------------------------------------------ 防误点 + 承认（纯判定 → 真写库）


def confirm_label(n: int) -> str:
    return f"承认 {n} 条（写库）"


def _dedupe(items: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = str(item)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def plan_ack(
    rows: Iterable[dict[str, Any]],
    selected_ids: Iterable[Any],
    *,
    typed_count: Optional[int] = None,
    by: str = "",
) -> dict[str, Any]:
    """**防误点**判定（纯函数，D5 §3.6）：勾选条数必须与人手确认的条数一致才放行。

    同时拦下三类不该承认的：不存在、``ESCALATED``/``REJECTED``、状态认不出。
    ``typed_count is None`` 表示调用方还没做人工确认 → 直接不放行（不默认等于勾选数）。
    """
    rows = list(rows)
    by_id = {row["request_id"]: row for row in rows}
    selected = _dedupe(selected_ids)
    refused: list[dict[str, Any]] = []

    for request_id in selected:
        row = by_id.get(request_id)
        if row is None:
            refused.append({"request_id": request_id, "reason": "台账里没有这个 request_id（拒绝凭空承认）"})
            continue
        if row["status"] in BLOCKED_STATUSES:
            refused.append(
                {
                    "request_id": request_id,
                    "reason": (
                        f"{row['status_label']} 不可被承认（D5 §3.2：ESCALATED/REJECTED 不许被 ack 翻白，"
                        "要翻必须新开请求）"
                    ),
                }
            )
            continue
        if row["status"] not in ACKABLE_STATUSES:
            refused.append(
                {"request_id": request_id, "reason": f"状态 `{row['status']}` 不在可承认集合 {ACKABLE_STATUSES}"}
            )

    accepted = [rid for rid in selected if all(item["request_id"] != rid for item in refused)]

    reasons: list[str] = []
    if not selected:
        reasons.append("未勾选任何请求")
    if not str(by or "").strip():
        reasons.append("缺少承认人（`--by`，留痕字段，不许空）")
    if typed_count is None:
        reasons.append("还没有人工确认条数（防误点：必须显式填一致条数）")
    elif int(typed_count) != len(selected):
        reasons.append(f"防误点：勾选 {len(selected)} 条，人工确认 {int(typed_count)} 条，不一致 → 不放行")
    if refused:
        reasons.append("有请求被拒（见 refused）")

    return {
        "ok": bool(accepted) and not reasons,
        "selected": selected,
        "accepted": accepted,
        "refused": refused,
        "blocked": [row["request_id"] for row in rows if row["blocked"]],
        "count": len(selected),
        "typed_count": typed_count,
        "by": str(by or "").strip(),
        "reasons": reasons,
    }


def build_acks_yaml(request_ids: Iterable[Any], *, note: str = "") -> str:
    """``batch-ack --file`` 的入参文本（D5 §3.2 命名的 acks.yaml）。

    D5 只冻结了命令、没冻结文件 schema —— 本函数是**唯一**一处形状定义，
    实现方若定成别的键，改这里一处即可（已在群里报备）。
    """
    lines = ["acks:"]
    for request_id in _dedupe(request_ids):
        lines.append(f"  - request_id: {request_id}")
        if note:
            lines.append(f"    note: {json.dumps(note, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _pick_batch(rows: list[dict[str, Any]], selected: list[str]) -> dict[str, Any]:
    wanted = set(selected)
    picked = [row for row in rows if row["request_id"] in wanted]
    first = picked[0] if picked else {}
    return {
        "batch_id": str(first.get("batch_id") or ""),
        "decided_by": str(first.get("decided_by") or ""),
        "decided_at": float(first.get("decided_at") or 0.0),
    }


def batch_ack(
    rows: Iterable[dict[str, Any]],
    selected_ids: Iterable[Any],
    *,
    by: str,
    typed_count: Optional[int] = None,
    note: str = "",
    db: Optional[str] = DEFAULT_DB,
    runner: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """**真承认**：``kfpa fastlane batch-ack --file acks.yaml --by <name>`` + 回读确认。

    返回字段：``ok`` / ``written`` / ``verified`` / ``batch_id`` / ``decided_by`` / ``decided_at``
    / ``acknowledged`` / ``refused`` / ``reasons`` / ``stdout``。
    ``ok`` 为真当且仅当：防误点过了 + CLI 退出码 0 + 回读看到状态翻成 ``approved`` 且 ``decided_by`` 相符。
    """
    rows = list(rows)
    verdict = plan_ack(rows, selected_ids, typed_count=typed_count, by=by)
    if not verdict["ok"]:
        verdict.update({"written": False, "verified": False, "acknowledged": [], "stdout": ""})
        return verdict

    payload = build_acks_yaml(verdict["accepted"], note=note)
    tmp_dir = tempfile.mkdtemp(prefix="kfpa-acks-")
    acks_path = Path(tmp_dir) / "acks.yaml"
    try:
        acks_path.write_text(payload, encoding="utf-8")
        argv = ["fastlane", "batch-ack", "--file", str(acks_path), "--by", verdict["by"]]
        if db:
            argv += ["--db", str(db)]
        rc, out, err = run_cli(runner, argv, db=db)
    except OSError as exc:
        verdict.update({"written": False, "verified": False, "acknowledged": [], "stdout": ""})
        verdict["reasons"] = [*verdict["reasons"], f"写 acks.yaml 失败：{exc}"]
        return verdict
    finally:
        try:
            acks_path.unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    result: dict[str, Any] = {
        **verdict,
        "written": False,
        "verified": False,
        "acknowledged": [],
        "stdout": (out or "").strip(),
        "stderr": (err or "").strip(),
    }
    if rc != 0:
        result["ok"] = False
        hint = FASTLANE_MISSING_HINT if rc == 2 else "核对 D5 §3.2 的 `fastlane batch-ack` 用法与其 acks 文件 schema"
        result["reasons"] = [*verdict["reasons"], f"`fastlane batch-ack` 退出码 {rc}：{(err or out).strip()[:400]} → {hint}"]
        return result

    result["written"] = True

    after, hint = list_requests(db=db, runner=runner)
    if hint:
        result["ok"] = False
        result["reasons"] = [*verdict["reasons"], f"写入已提交但回读失败（不谎报成功）：{hint}"]
        return result

    by_name = {row["request_id"]: row for row in after}
    unverified: list[dict[str, str]] = []
    for request_id in verdict["accepted"]:
        now = by_name.get(request_id)
        if now is None:
            unverified.append({"request_id": request_id, "reason": "回读台账里找不到该请求"})
        elif now["status"] != "approved":
            unverified.append({"request_id": request_id, "reason": f"回读状态仍是 `{now['status']}`"})
        elif now["decided_by"] and now["decided_by"] != verdict["by"]:
            unverified.append(
                {"request_id": request_id, "reason": f"回读 decided_by=`{now['decided_by']}` 与承认人 `{verdict['by']}` 不符"}
            )

    result.update(_pick_batch(after, verdict["accepted"]))
    result["verified"] = not unverified
    result["unverified"] = unverified
    result["acknowledged"] = [by_name.get(rid) or {"request_id": rid} for rid in verdict["accepted"]]
    result["ok"] = not unverified
    if unverified:
        result["reasons"] = [*verdict["reasons"], "CLI 说写过，但回读未确认 → 拒绝报成功（见 unverified）"]
    return result


# ------------------------------------------------------------------ 展示（streamlit）


def _table_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "请求号": row["request_id"],
            "轮次": row["round_no"],
            "摘要": row["summary"],
            "状态": row["status_label"],
            "批次": row["batch_id"] or "-",
            "承认人": row["decided_by"] or "-",
            "承认时间": _fmt_time(row["decided_at"]),
            "改动文件": "、".join(row["diff_files"]) or "-",
        }
        for row in rows
    ]


def render(
    st,
    rows: list[dict[str, Any]],
    *,
    ack: Optional[Callable[[list[str], int], dict[str, Any]]] = None,
    by: str = "",
    key_prefix: str = "fastlane-ack",
    report_dir: Optional[str] = DEFAULT_OUT_DIR,
) -> dict[str, Any]:
    """Streamlit 段落：台账 + 防误点勾选 + 真承认；返回本次判定/写入结果（headless 可断言）。

    ``ack`` 为 None 时只做**判定预演**（不写库），用于无库冒烟。
    """
    st.subheader("快车道承认（P3：真入库）")
    st.info(ACK_BANNER)
    st.caption("判据（D5 §3.2）：不改接口契约 / 不改数据模型 / 波及 ≤1 模块；本页只管承认与留痕。")

    if not rows:
        st.info("台账里没有快车道请求（`kfpa fastlane plan --round N` 才会登记）。")
        return {"ok": False, "rows": 0, "reason": "台账为空"}

    st.dataframe(_table_rows(rows), width="stretch")

    blocked = [row for row in rows if row["blocked"]]
    if blocked:
        st.warning(
            "以下请求已升级/拒绝，**不可**被承认（要翻必须新开请求）："
            + "、".join(f"{row['request_id']}（{row['status_label']}）" for row in blocked)
        )

    candidates = [row["request_id"] for row in rows if row["ackable"]]
    if not candidates:
        st.info("当前没有可承认的请求（可承认状态：" + "、".join(ACKABLE_STATUSES) + "）。")
        return {"ok": False, "rows": len(rows), "reason": "无可承认请求"}

    picked = st.multiselect("勾选本批要承认的请求（一批一个 batch_id）", candidates, key=f"{key_prefix}-picked")
    actor = st.text_input("承认人（decided_by，留痕）", value=by, key=f"{key_prefix}-by")
    typed = st.number_input("人工确认条数（必须与勾选数一致，防误点）", min_value=0, step=1, key=f"{key_prefix}-count")
    note = st.text_input("备注（可选，写进 acks 文件）", key=f"{key_prefix}-note")

    plan = plan_ack(rows, picked, typed_count=int(typed) if typed else None, by=actor)
    for reason in plan["reasons"]:
        st.caption(f"· {reason}")
    for item in plan["refused"]:
        st.warning(f"{item['request_id']} 被拒：{item['reason']}")

    if st.button(confirm_label(len(picked)), key=f"{key_prefix}-ack", disabled=not plan["ok"]):
        if ack is None:
            st.info("判定预演：没有接线写库动作（本页只给 plan_ack 的结论）。")
            return {**plan, "written": False, "verified": False}
        result = ack(picked, int(typed))
        if result.get("ok"):
            st.success(
                f"已承认 {len(result.get('acknowledged', []))} 条"
                f"（batch_id={result.get('batch_id') or '-'}，"
                f"decided_by={result.get('decided_by') or '-'}，"
                f"decided_at={_fmt_time(result.get('decided_at'))}）"
                f"{_CMD_SEP}回读已确认"
            )
            if report_dir:
                written = write_reports(rows, batch_id=result.get("batch_id") or "", out_dir=report_dir)
                if written.get("files"):
                    st.caption("台账报告：" + "、".join(written["files"]))
        else:
            st.error("承认没有落库（本页不谎报成功）：" + "；".join(result.get("reasons") or ["未知原因"]))
            for item in result.get("unverified", []):
                st.warning(f"{item['request_id']} 未确认：{item['reason']}")
        st.rerun()
    return plan


# ------------------------------------------------------------------ 报告（单文件 / 离线）


_FILE_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(value: str, *, fallback: str = "batch-unknown") -> str:
    """批次名 → 文件名安全片段（防路径穿越，D5 §3.6）。"""
    cleaned = _FILE_UNSAFE.sub("-", str(value or "")).strip("-.")
    return cleaned[:64] or fallback


def report_stem(rows: Iterable[dict[str, Any]], *, batch_id: str = "") -> str:
    if batch_id:
        return f"fastlane-{safe_stem(batch_id, fallback='batch')}"
    batches = sorted({str(row.get("batch_id")) for row in rows if row.get("batch_id")})
    if len(batches) == 1:
        return f"fastlane-{safe_stem(batches[0], fallback='batch')}"
    return "fastlane-all"


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = status_value(row.get("status"))
        out[key] = out.get(key, 0) + 1
    return out


def render_markdown(
    rows: Iterable[dict[str, Any]],
    *,
    batch_id: str = "",
    generated_at: Optional[float] = None,
) -> str:
    """承认台账 Markdown（离线可读）。"""
    rows = list(rows)
    stamp = time.time() if generated_at is None else generated_at
    counts = _counts(rows)
    lines = [
        f"# 快车道承认台账{f'（批次 {batch_id}）' if batch_id else ''}",
        "",
        f"- 生成时间：{_fmt_time(stamp)}",
        f"- 请求总数：{len(rows)}",
        "- 状态计数：" + ("；".join(f"{status_label(k)} {v}" for k, v in sorted(counts.items())) or "-"),
        "- 口径：D5 §3.2（`batch_id` / `decided_by` / `decided_at` 来自 `fastlane_requests` 落库；"
        "`ESCALATED`/`REJECTED` 不许被承认）",
        "",
        "| 请求号 | 轮次 | 状态 | 批次 | 承认人 | 承认时间 | 摘要 | 改动文件 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _md_cell(value)
                for value in (
                    row.get("request_id", ""),
                    row.get("round_no", ""),
                    row.get("status_label") or status_label(row.get("status")),
                    row.get("batch_id") or "-",
                    row.get("decided_by") or "-",
                    _fmt_time(row.get("decided_at")),
                    row.get("summary", ""),
                    "、".join(row.get("diff_files") or []) or "-",
                )
            )
            + " |"
        )
    lines += ["", "> 本报告由 `kfpa.ui.fastlane_page` 渲染；判据本身由 `kfpa.impact` 出（D3 §3.6）。", ""]
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


_HTML_CSS = """
:root { color-scheme: light; }
body { font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; margin: 2rem auto; max-width: 1100px; color: #1b1b1f; }
h1 { font-size: 1.5rem; border-bottom: 2px solid #444; padding-bottom: .4rem; }
h2 { font-size: 1.1rem; margin-top: 1.6rem; }
table { border-collapse: collapse; width: 100%; font-size: .86rem; }
th, td { border: 1px solid #bbb; padding: .35rem .5rem; text-align: left; vertical-align: top; }
th { background: #f0f0f3; }
tr:nth-child(even) td { background: #fafafa; }
.meta { font-size: .85rem; color: #444; }
.badge { display: inline-block; padding: .05rem .4rem; border: 1px solid #888; border-radius: .25rem; font-size: .78rem; }
.note { font-size: .8rem; color: #555; margin-top: 1.2rem; }
@media print { body { margin: 0; max-width: none; } a { color: inherit; text-decoration: none; } }
"""


def render_html(
    rows: Iterable[dict[str, Any]],
    *,
    batch_id: str = "",
    title: str = "",
    generated_at: Optional[float] = None,
) -> str:
    """单文件 HTML（内联 CSS、无 CDN、无外链、无脚本，可离线打印；D5 §3.6）。"""
    rows = list(rows)
    stamp = time.time() if generated_at is None else generated_at
    counts = _counts(rows)
    heading = title or f"快车道承认台账{f'（批次 {batch_id}）' if batch_id else ''}"
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(heading)}</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{html.escape(heading)}</h1>",
        '<p class="meta">'
        f"生成时间：{html.escape(_fmt_time(stamp))} · 请求总数：{len(rows)} · "
        + html.escape("；".join(f"{status_label(k)} {v}" for k, v in sorted(counts.items())) or "无请求")
        + "</p>",
        "<h2>台账</h2>",
        "<table><thead><tr>"
        + "".join(
            f"<th>{html.escape(name)}</th>"
            for name in ("请求号", "轮次", "状态", "批次", "承认人", "承认时间", "摘要", "改动文件")
        )
        + "</tr></thead><tbody>",
    ]
    for row in rows:
        cells = (
            row.get("request_id", ""),
            row.get("round_no", ""),
            row.get("status_label") or status_label(row.get("status")),
            row.get("batch_id") or "-",
            row.get("decided_by") or "-",
            _fmt_time(row.get("decided_at")),
            row.get("summary", ""),
            "、".join(row.get("diff_files") or []) or "-",
        )
        parts.append("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in cells) + "</tr>")
    parts += [
        "</tbody></table>",
        '<p class="note">口径：D5 §3.2 —— ',
        "承认留痕取自 <code>fastlane_requests</code>；<code>ESCALATED</code>/<code>REJECTED</code> 不许被翻成 <code>approved</code>。 ",
        "本文件单文件离线可打印，无外部依赖。</p>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(parts)


def render_json(rows: Iterable[dict[str, Any]], *, batch_id: str = "") -> str:
    rows = list(rows)
    payload = {
        "batch_id": batch_id,
        "generated_at": time.time(),
        "counts": _counts(rows),
        "requests": rows,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def write_reports(
    rows: Iterable[dict[str, Any]],
    *,
    batch_id: str = "",
    out_dir: str | Path = DEFAULT_OUT_DIR,
    formats: Iterable[str] = ("md", "html"),
) -> dict[str, Any]:
    """落 ``reports/fastlane-<批次>.{md,html}``（D5 §3.6）。不覆盖别的文件、失败不抛。"""
    rows = list(rows)
    stem = report_stem(rows, batch_id=batch_id)
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
            path = directory / f"{stem}.{fmt}"
            text = render_markdown(rows, batch_id=batch_id) if fmt == "md" else render_html(rows, batch_id=batch_id)
            path.write_text(text, encoding="utf-8")
            files.append(str(path))
        except OSError as exc:
            errors.append(f"{fmt} 写入失败：{exc} → 检查目录权限")
    return {"stem": stem, "files": files, "errors": errors}


# ------------------------------------------------------------------ headless


def render_headless(
    *,
    db: Optional[str] = DEFAULT_DB,
    rows: Optional[list[dict[str, Any]]] = None,
    runner: Optional[Callable[..., Any]] = None,
    out_dir: Optional[str] = None,
) -> dict[str, Any]:
    """不进浏览器的一次渲染：取数（或吃现成行）+ 统计 + 可选落报告。"""
    hint = ""
    if rows is None:
        rows, hint = list_requests(db=db, runner=runner)
    rows = list(rows)
    summary: dict[str, Any] = {
        "rows": len(rows),
        "counts": _counts(rows),
        "ackable": [row["request_id"] for row in rows if row["ackable"]],
        "blocked": [row["request_id"] for row in rows if row["blocked"]],
        "batches": sorted({row["batch_id"] for row in rows if row["batch_id"]}),
        "hint": hint,
        "reports": [],
    }
    if out_dir and rows:
        written = write_reports(rows, out_dir=out_dir)
        summary["reports"] = written["files"]
        if written["errors"]:
            summary["report_errors"] = written["errors"]
    return summary


# ------------------------------------------------------------------ 模块入口


def main(argv: Optional[list[str]] = None, *, runner: Optional[Callable[..., Any]] = None) -> int:
    """``python -m kfpa.ui.fastlane_page --db … --out-dir reports``（台账 → 报告）。"""
    import argparse

    parser = argparse.ArgumentParser(prog="kfpa-ui-fastlane", description="快车道承认台账与报告渲染（P3）")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--status", default=None, help="只看某状态（pending/approved/rejected/escalated）")
    parser.add_argument("--batch", default="", help="只渲染该批次")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--stdout", action="store_true", help="打印台账而不是只写文件")
    parser.add_argument("--format", choices=("md", "html", "both", "none"), default="both")
    parser.add_argument("--json", action="store_true", help="打印机器可读摘要")
    args = parser.parse_args(argv)

    rows, hint = list_requests(db=args.db, status=args.status, runner=runner)
    if hint:
        print(hint)
        # 子命令没挂（rc=2）→ 用法/环境问题：rc=4（R-31 三分口径的「合法但缺依赖」一侧）
        return 4 if FASTLANE_MISSING_HINT == hint else 5
    if args.batch:
        rows = [row for row in rows if str(row.get("batch_id") or "") == args.batch]

    files: list[str] = []
    if args.format != "none":
        formats = ("md", "html") if args.format == "both" else (args.format,)
        written = write_reports(rows, batch_id=args.batch, out_dir=args.out_dir, formats=formats)
        files = written["files"]
        for error in written["errors"]:
            print(error)

    if args.json:
        print(json.dumps({"counts": _counts(rows), "files": files, "rows": len(rows)}, ensure_ascii=False))
    elif args.stdout:
        print(render_markdown(rows, batch_id=args.batch))
    else:
        print(f"台账 {len(rows)} 条；报告：{'、'.join(files) or '（未写）'}")
    return 0 if not files or all(Path(f).exists() for f in files) else 5


if __name__ == "__main__":  # pragma: no cover - 模块入口
    raise SystemExit(main())

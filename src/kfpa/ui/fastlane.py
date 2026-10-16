"""快车道攒批承认界面（D3 §2 / §3.6 / §3.7）。

职责：把「这一轮改动是否够小、能不能走快车道」的判定结果摊到人面前，让人攒批承认。
判定本身不在这里 —— 走长流的纯函数 ``kfpa.impact.analyze(task_package, diff_files)``
（D3 §3.6，判据三条全真才 ``fast_lane=True``）；本模块只做取数、展示、攒批确认。

设计约束：

- **数据层不 import streamlit**：``load_reports`` / ``acknowledge`` 可直接 headless 调用；
- ``kfpa.impact`` 惰性导入：模块还没就绪时返回空列表 + 中文提示，不让页面崩；
- ``fast_lane=False`` 的行**不允许承认**，必须提示「升级回完整设计回合」（D3 §3.6）；
- 承认动作 P1 **没有落库接口**（D3 §3.2 的 Store 只定义了 ledger/knowledge 三个新方法），
  因此默认只在进程内确认；需要留痕时用 ``ack_file`` 追加 JSONL（可选，不写库）。

登记给 Chief 的两处空白（本模块用参数/提示兜住，不擅自扩契约）：

1. 快车道承认结果没有持久化接口（谁承认、何时、承认了哪轮）；
2. ``diff_files`` 没有来源 —— P0/P1 的 Store 都不存 ``ExecResult``，只能由调用方传入
   （页面侧支持 ``--diff-files <json>``：``{"1": ["src/kfpa/gates.py"]}``）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["load_reports", "acknowledge", "render", "parse_diff_files", "IMPACT_MISSING_HINT"]

#: kfpa.impact 未就绪时的中文提示（D3 §3.6 归长流）
IMPACT_MISSING_HINT = (
    "[kfpa-ui] 缺少 kfpa.impact（快车道影响分析，D3 §3.6）→ 本次不做快车道判定；"
    "等该模块合入 main 后重跑即可。"
)

_CRITERION_NAMES = (
    "no_interface_change",
    "no_model_change",
    "scope_ok",
    "fast_lane",
    "reason",
    "boundaries",
    "touched_boundaries",
    "reasons",
    "touched",
)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _iter_rounds(store) -> list[int]:
    """能枚举的轮次：快照 + 票面（P1 Store 没有 list_rounds，沿用既有方法）。"""
    rounds: set[int] = set()
    try:
        for snapshot in store.list_snapshots():
            rounds.add(int(getattr(snapshot, "round_no", 0) or 0))
    except Exception:  # 快照表不可读不影响票面来源
        pass
    try:
        for ticket in store.list_tickets():
            rounds.add(int(getattr(ticket, "round_no", 0) or 0))
    except Exception:
        pass
    return sorted(r for r in rounds if r > 0)


def parse_diff_files(payload: str | bytes | None) -> dict[int, list[str]]:
    """解析 ``{"1": ["src/kfpa/gates.py", ...]}``；键/值都宽松处理，坏数据不抛。"""
    if not payload:
        return {}
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[int, list[str]] = {}
    for key, value in raw.items():
        try:
            round_no = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            out[round_no] = [str(v) for v in value]
    return out


def _load_impact():
    """惰性拿 ``kfpa.impact``；未就绪返回 None。"""
    try:
        from kfpa import impact as impact_mod  # type: ignore[import-not-found]
    except ImportError:
        return None
    return impact_mod


def _criteria_of(report: Any) -> dict[str, Any]:
    """从 ImpactReport 里取可展示的判据（字段名按 D3 §3.6 宽松兼容）。"""
    out: dict[str, Any] = {}
    for name in _CRITERION_NAMES:
        if hasattr(report, name):
            value = getattr(report, name)
            out[name] = value if isinstance(value, (bool, str, int, float)) else list(value)
    return out


def load_reports(
    store,
    *,
    diff_files_by_round: Optional[dict[int, list[str]]] = None,
    impact_mod=None,
) -> tuple[list[dict[str, Any]], str]:
    """返回 ``(待承认行, 提示)``；``kfpa.impact`` 未就绪 → ``([], 中文提示)``。"""
    module = impact_mod if impact_mod is not None else _load_impact()
    if module is None:
        return [], IMPACT_MISSING_HINT

    diff_files_by_round = diff_files_by_round or {}
    hints: list[str] = []
    rows: list[dict[str, Any]] = []
    for round_no in _iter_rounds(store):
        try:
            package = store.get_package(round_no)
        except Exception as exc:  # 单轮读包失败不拖垮整页
            hints.append(f"第 {round_no} 轮任务包读取失败：{exc}")
            continue
        if package is None:
            continue
        diff_files = list(diff_files_by_round.get(round_no, []))
        try:
            report = module.analyze(package, diff_files)
        except Exception as exc:
            hints.append(
                f"第 {round_no} 轮影响分析失败：{exc} → 核对 kfpa.impact.analyze(package, diff_files)（D3 §3.6）"
            )
            continue
        criteria = _criteria_of(report)
        fast_lane = bool(criteria.get("fast_lane", getattr(report, "fast_lane", False)))
        boundaries = criteria.get("boundaries") or criteria.get("touched_boundaries") or []
        if isinstance(boundaries, str):
            boundaries = [boundaries]
        reason = criteria.get("reason") or ""
        if not reason and not fast_lane and criteria.get("reasons"):
            reason = "；".join(str(r) for r in criteria.get("reasons") or [])
        rows.append(
            {
                "round": round_no,
                "feature": str(getattr(package, "feature", "") or ""),
                "fast_lane": fast_lane,
                "boundaries": [str(b) for b in boundaries],
                "reason": str(reason),
                "diff_files": diff_files,
                "criteria": {k: v for k, v in criteria.items() if isinstance(v, bool)},
                "acknowledged": False,
                "at": 0.0,
            }
        )
    return rows, "；".join(hints)


def acknowledge(
    rows: Iterable[dict[str, Any]],
    rounds: Iterable[int],
    *,
    actor: str = "",
    note: str = "",
    ack_file: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """攒批承认：只承认 ``fast_lane=True`` 的行；其余进 ``refused`` 并要求回完整回合。"""
    stamp = time.time() if now is None else now
    wanted = [int(r) for r in rounds]
    by_round = {int(r["round"]): r for r in rows}
    acknowledged: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []

    for round_no in wanted:
        row = by_round.get(round_no)
        if row is None:
            refused.append({"round": round_no, "reason": "没有该轮的快车道判定"})
            continue
        if not row.get("fast_lane"):
            refused.append(
                {
                    "round": round_no,
                    "reason": "三判据未全真 → 必须升级回完整设计回合（D3 §3.6）",
                }
            )
            continue
        row = dict(row)
        row["acknowledged"] = True
        row["at"] = stamp
        row["actor"] = actor
        row["note"] = note
        acknowledged.append(row)

    result = {
        "acknowledged": acknowledged,
        "refused": refused,
        "actor": actor,
        "note": note,
        "at": stamp,
        "persisted": False,
        "hint": "P1 无承认落库接口（D3 §3.2）：仅在进程内确认，需要留痕请给 ack_file。",
    }

    if ack_file:
        path = Path(ack_file)
        result["persisted"] = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            result["persisted"] = False
            result["hint"] = f"承认留痕失败：{exc} → 检查 ack_file 路径与权限"

    return result


def _table_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "轮次": r["round"],
            "功能": r["feature"],
            "快车道": "是" if r["fast_lane"] else "否（回完整回合）",
            "边界": "、".join(r["boundaries"]) if r["boundaries"] else "-",
            "说明": r["reason"],
        }
        for r in rows
    ]


def render(st, rows: list[dict[str, Any]], *, ack=None, key_prefix: str = "fastlane") -> list[int]:
    """Streamlit 段落：展示判定 + 攒批承认；返回被选中的轮次（headless 可断言）。"""
    st.subheader("快车道攒批承认")
    st.caption("三判据（接口契约 / 数据模型 / 波及面）全真才可承认；否则必须回完整设计回合（D3 §3.6）。")

    if not rows:
        st.info("没有快车道候选（或 kfpa.impact 尚未就绪）。")
        return []

    st.dataframe(_table_rows(rows), width="stretch")

    candidates = [r["round"] for r in rows if r["fast_lane"]]
    blocked = [r["round"] for r in rows if not r["fast_lane"]]
    if blocked:
        st.warning("以下轮次不允许走快车道，请升级回完整设计回合：" + "、".join(f"第 {r} 轮" for r in blocked))
    if not candidates:
        return []

    picked = st.multiselect(
        "选择要承认的轮次",
        candidates,
        default=candidates,
        key=f"{key_prefix}-picked",
    )
    note = st.text_area("承认备注（会写进留痕）", key=f"{key_prefix}-note")
    if st.button(f"承认并放行 {len(picked)} 轮", key=f"{key_prefix}-ack"):
        if ack is None:
            st.warning("没有接线承认动作（P1 无落库接口）→ 未做任何确认。")
        else:
            result = ack(picked, note)
            done = [r["round"] for r in result.get("acknowledged", [])]
            st.success(f"已承认 {len(done)} 轮：" + "、".join(f"第 {r} 轮" for r in done))
            for item in result.get("refused", []):
                st.warning(f"第 {item['round']} 轮被拒：{item['reason']}")
            st.caption(result.get("hint", ""))
            st.rerun()
    return picked

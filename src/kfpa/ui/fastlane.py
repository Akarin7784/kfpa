"""快车道**预览**页（D3 §2 / §3.6 / §3.7 + R-23）。

职责：把「这一轮改动是否够小、能不能走快车道」的判定结果摊到人面前做**预览**。
判定本身不在这里 —— 走长流的纯函数 ``kfpa.impact.analyze(task_package, diff_files)``
（D3 §3.6，判据三条全真才 ``fast_lane=True``）；本模块只做取数、展示与预览确认。

R-23 两条硬口径：

1. **本模块是历史预览层**：R-23 要求 P1 不落库，本模块保留为判定预演；
   P3 的真承认（写 ``fastlane_requests``、留 ``batch_id/decided_by/decided_at``）在
   ``kfpa/ui/fastlane_page.py``（D5 §3.2/§3.6）。本模块任何按钮文案仍必须写明「不写库」
   （``acknowledge()`` 只返回预览结果）。
2. **``diff_files`` 三来源优先级**：``ExecResult.files_changed`` → 显式 ``--files`` →
   都拿不到则 ``fast_lane=None`` + ``reason="no_diff_source"``（**不猜、不默认 True**）。
   界面预览另有展示层来源 ``git diff --name-only HEAD``（默认关闭，需显式开启），
   它只作展示、且在页面上明确标注「CLI 判定不依赖 git」。

设计约束：数据层不 import streamlit（``load_reports`` / ``resolve_diff_files`` /
``acknowledge`` 都可 headless 调用）；``kfpa.impact`` 惰性导入，未就绪时返回空列表 + 中文提示。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

__all__ = [
    "load_reports",
    "resolve_diff_files",
    "acknowledge",
    "render",
    "parse_diff_files",
    "IMPACT_MISSING_HINT",
    "PREVIEW_BANNER",
    "ACK_BUTTON_LABEL",
    "NO_DIFF_SOURCE",
    "SOURCE_EXEC",
    "SOURCE_CLI",
    "SOURCE_GIT",
]

#: kfpa.impact 未就绪时的中文提示（D3 §3.6 归长流）
IMPACT_MISSING_HINT = (
    "[kfpa-ui] 缺少 kfpa.impact（快车道影响分析，D3 §3.6）→ 本次不做快车道判定；"
    "等该模块合入 main 后重跑即可。"
)

#: R-23 的历史口径：**本模块**仍是纯预演（不写库）；P3 的真承认在 ``kfpa/ui/fastlane_page.py``
PREVIEW_BANNER = (
    "R-23 的历史口径：本模块只做判定预演，不写库、不新增表 —— P3 的**真承认**"
    "（写 fastlane_requests，留 batch_id/decided_by/decided_at）在 `kfpa/ui/fastlane_page.py`（D5 §3.2/§3.6）。"
)
#: R-23 要求的按钮文案（本模块是预演，别让它看起来像入库）
ACK_BUTTON_LABEL = "预演（不写库）：确认放行 {n} 轮 —— 入库请用快车道承认页（P3）"

#: 判定来源（R-23 三来源 + 界面展示层）
SOURCE_EXEC = "exec_result"
SOURCE_CLI = "cli_files"
SOURCE_GIT = "git_preview"
SOURCE_EXPLICIT = "explicit"
NO_DIFF_SOURCE = "no_diff_source"

_SOURCE_LABEL = {
    SOURCE_EXEC: "执行产物 ExecResult.files_changed",
    SOURCE_CLI: "显式 --files",
    SOURCE_GIT: "git 工作树（展示层，非判定依据）",
    SOURCE_EXPLICIT: "调用方显式给出",
    NO_DIFF_SOURCE: "无 diff 来源（未判定）",
}

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

_EXEC_GETTERS = ("get_exec_result", "latest_exec_result", "list_exec_results", "get_exec_results")


# ------------------------------------------------------------------ 基础


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _iter_rounds(store) -> list[int]:
    """能枚举的轮次：快照 + 票面（Store 没有 list_rounds，沿用既有方法）。"""
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


# ------------------------------------------------------------------ diff_files 三来源（R-23）


def _files_of(value: Any, round_no: int) -> list[str]:
    """从 ExecResult / 其结果集合里抽 ``files_changed``（宽松解析，不抛）。"""
    if value is None:
        return []
    if isinstance(value, dict):
        raw = value.get("files_changed") or value.get("files") or []
        return [str(f) for f in raw]
    if isinstance(value, (list, tuple)):
        for item in value:
            if int(getattr(item, "round_no", round_no) or 0) == round_no:
                files = _files_of(item, round_no)
                if files:
                    return files
        for item in value:
            files = _files_of(item, round_no)
            if files:
                return files
        return []
    raw = getattr(value, "files_changed", None)
    if raw is None:
        raw = getattr(value, "files", None)
    return [str(f) for f in (raw or [])]


def _store_exec_files(store, round_no: int) -> list[str]:
    """来源 1：本轮执行器真实产出（Store 若提供相应读取接口）。

    P1 的 Store 目前不落 ``ExecResult``，所以正常会落空 —— 这里不猜、不合成，
    拿不到就交给下一个来源（R-23）。
    """
    for name in _EXEC_GETTERS:
        getter = getattr(store, name, None)
        if not callable(getter):
            continue
        try:
            value = getter(round_no)
        except TypeError:
            try:
                value = getter()
            except Exception:
                continue
        except Exception:
            continue
        files = _files_of(value, round_no)
        if files:
            return files
    return []


def _split_files(text: Any) -> list[str]:
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        return [str(item).strip() for item in text if str(item).strip()]
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def _git_diff_files(git_runner: Optional[Callable[[], Any]] = None) -> list[str]:
    """展示层来源：``git diff --name-only HEAD``（R-23：CLI 判定不得依赖 git 状态）。"""
    if git_runner is not None:
        try:
            return _split_files(git_runner())
        except Exception:
            return []
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return _split_files(proc.stdout)


def resolve_diff_files(
    store,
    round_no: int,
    *,
    cli_files: Optional[Iterable[str]] = None,
    use_git: bool = False,
    git_runner: Optional[Callable[[], Any]] = None,
) -> tuple[Optional[list[str]], str]:
    """按 R-23 优先级取 diff 文件。

    返回 ``(files, source)``；``files is None`` 表示拿不到来源，调用方必须判
    ``fast_lane=None`` + ``reason="no_diff_source"``，不得默认 True。
    """
    files = _store_exec_files(store, round_no)
    if files:
        return files, SOURCE_EXEC
    if cli_files:
        return [str(f) for f in cli_files], SOURCE_CLI
    if use_git:
        files = _git_diff_files(git_runner)
        if files:
            return files, SOURCE_GIT
    return None, NO_DIFF_SOURCE


# ------------------------------------------------------------------ 取数


def _criteria_of(report: Any) -> dict[str, Any]:
    """从 ImpactReport 里取可展示的判据（字段名按 D3 §3.6 宽松兼容）。"""
    out: dict[str, Any] = {}
    for name in _CRITERION_NAMES:
        if hasattr(report, name):
            value = getattr(report, name)
            out[name] = value if isinstance(value, (bool, str, int, float)) or value is None else list(value)
    return out


def load_reports(
    store,
    *,
    diff_files_by_round: Optional[dict[int, list[str]]] = None,
    cli_files: Optional[Iterable[str]] = None,
    use_git: bool = False,
    git_runner: Optional[Callable[[], Any]] = None,
    impact_mod=None,
) -> tuple[list[dict[str, Any]], str]:
    """返回 ``(预览行, 提示)``；``kfpa.impact`` 未就绪 → ``([], 中文提示)``。"""
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

        if round_no in diff_files_by_round:
            files: Optional[list[str]] = list(diff_files_by_round[round_no])
            source = SOURCE_EXPLICIT
        else:
            files, source = resolve_diff_files(
                store, round_no, cli_files=cli_files, use_git=use_git, git_runner=git_runner
            )

        base = {
            "round": round_no,
            "feature": str(getattr(package, "feature", "") or ""),
            "diff_files": list(files or []),
            "source": source,
            "preview": True,
        }

        if files is None:
            # R-23：不猜、不默认 True
            base.update({"fast_lane": None, "reason": NO_DIFF_SOURCE, "boundaries": [], "criteria": {}})
            rows.append(base)
            continue

        try:
            report = module.analyze(package, files)
        except Exception as exc:
            hints.append(
                f"第 {round_no} 轮影响分析失败：{exc} → 核对 kfpa.impact.analyze(package, diff_files)（D3 §3.6）"
            )
            continue

        criteria = _criteria_of(report)
        state = criteria.get("fast_lane", getattr(report, "fast_lane", None))
        fast_lane = None if state is None else bool(state)
        boundaries = criteria.get("boundaries") or criteria.get("touched_boundaries") or []
        if isinstance(boundaries, str):
            boundaries = [boundaries]
        reason = criteria.get("reason") or ""
        if not reason and not fast_lane and criteria.get("reasons"):
            reason = "；".join(str(r) for r in criteria.get("reasons") or [])
        base.update(
            {
                "fast_lane": fast_lane,
                "boundaries": [str(b) for b in boundaries],
                "reason": str(reason),
                "criteria": {k: v for k, v in criteria.items() if isinstance(v, bool)},
            }
        )
        rows.append(base)

    return rows, "；".join(hints)


# ------------------------------------------------------------------ 预览确认（不落库）


def acknowledge(
    rows: Iterable[dict[str, Any]],
    rounds: Iterable[int],
    *,
    actor: str = "",
    note: str = "",
    preview_log: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """**预览**确认（R-23）：只算出「若 P3 落库时会承认哪些」，不写库、不新增表。

    只认 ``fast_lane is True`` 的行；``None``（无 diff 来源）与 ``False``（判据未全真）都进 ``refused``。
    ``preview_log`` 只是本地 JSONL 留痕（可选），**不是入库**。
    """
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
        state = row.get("fast_lane")
        if state is None:
            refused.append(
                {"round": round_no, "reason": "无 diff 来源（no_diff_source）→ 判据未定，不猜（R-23）"}
            )
            continue
        if not state:
            refused.append(
                {"round": round_no, "reason": "三判据未全真 → 必须升级回完整设计回合（D3 §3.6）"}
            )
            continue
        item = dict(row)
        item.update({"preview": True, "would_acknowledge": True, "actor": actor, "note": note, "at": stamp})
        acknowledged.append(item)

    result: dict[str, Any] = {
        "preview": True,
        "acknowledged": acknowledged,
        "refused": refused,
        "actor": actor,
        "note": note,
        "at": stamp,
        "hint": "本模块只做预演，不写库；P3 的真承认走 kfpa/ui/fastlane_page.py（D5 §3.2）。",
    }

    if preview_log:
        path = Path(preview_log)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
            result["preview_log"] = str(path)
        except OSError as exc:
            result["preview_log_error"] = f"{exc} → 检查路径与权限"

    return result


# ------------------------------------------------------------------ 展示


def _state_label(row: dict[str, Any]) -> str:
    state = row.get("fast_lane")
    if state is None:
        return "未判定（无 diff 来源）"
    return "可走快车道" if state else "否（回完整回合）"


def _table_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "轮次": row["round"],
            "功能": row["feature"],
            "判定": _state_label(row),
            "判定来源": _SOURCE_LABEL.get(row.get("source", ""), row.get("source", "")),
            "边界": "、".join(row["boundaries"]) if row.get("boundaries") else "-",
            "说明": row.get("reason", ""),
        }
        for row in rows
    ]


def render(st, rows: list[dict[str, Any]], *, ack=None, key_prefix: str = "fastlane") -> list[int]:
    """Streamlit 段落：**预览**判定 + 勾选 + 预览确认；返回被选中的轮次（headless 可断言）。"""
    st.subheader("快车道判定预演（不写库）")
    st.info(PREVIEW_BANNER)
    st.caption("判据（D3 §3.6）：不改接口契约 / 不改数据模型 / 波及 ≤1 模块，三者全真才可走快车道。")

    if not rows:
        st.info("没有快车道候选（或 kfpa.impact 尚未就绪）。")
        return []

    st.dataframe(_table_rows(rows), width="stretch")

    if any(row.get("source") == SOURCE_GIT for row in rows):
        st.warning(
            "有轮次的判据来自 git 工作树（展示层）——脏工作树会给出假判据，"
            "CLI 判定不依赖 git（R-23）。"
        )

    undecided = [row["round"] for row in rows if row.get("fast_lane") is None]
    blocked = [row["round"] for row in rows if row.get("fast_lane") is False]
    if undecided:
        st.warning("以下轮次没有 diff 来源，判据未定，不猜：" + "、".join(f"第 {r} 轮" for r in undecided))
    if blocked:
        st.warning("以下轮次不允许走快车道，请升级回完整设计回合：" + "、".join(f"第 {r} 轮" for r in blocked))

    candidates = [row["round"] for row in rows if row.get("fast_lane") is True]
    if not candidates:
        return []

    picked = st.multiselect(
        "选择要预览确认的轮次",
        candidates,
        default=candidates,
        key=f"{key_prefix}-picked",
    )
    note = st.text_area("预览备注（P3 落库时一并写入）", key=f"{key_prefix}-note")
    if st.button(ACK_BUTTON_LABEL.format(n=len(picked)), key=f"{key_prefix}-ack"):
        if ack is None:
            st.info("这是预览：没有接线任何入库动作（R-23）。")
        else:
            result = ack(picked, note)
            done = [item["round"] for item in result.get("acknowledged", [])]
            st.info("预览结果：若 P3 落库，将承认 " + ("、".join(f"第 {r} 轮" for r in done) or "无"))
            for item in result.get("refused", []):
                st.warning(f"第 {item['round']} 轮被拒：{item['reason']}")
            st.caption(result.get("hint", PREVIEW_BANNER))
            st.rerun()
    return picked

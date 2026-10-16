r"""质量门禁三态判定（纯函数，P0，契约 §3.5 + 设计补充件 D1-3 / D1-4）。

判定顺序**严格按 D1-3**，短路返回，**不允许 LLM 参与任何判定**：

| 序 | 条件 | 结果 |
|---|---|---|
| G1 | `package.missing_sections()` 非空（或验收条目缺 cid/text） | NEEDS_FIX，failing_criteria = 缺段名 |
| G2 | `exec_result.ok is False` | NEEDS_FIX，按 `error_category` 定类 |
| G3 | `exec_result.failed_criteria` 非空 | NEEDS_FIX，逐条填 failing_criteria |
| G4 | `acceptance` 中存在 cid 不在 `exec_result.results` 里 | NEEDS_FIX，category=unmapped_criterion |
| G5 | `count_same_failure(round, reason_key) + 1 >= 2` | BLOCKED，escalated=True |
| G6 | 以上皆否 | PASS |

失败归一化（D1-4）：
    category 白名单 apply_failed | compile | test_fail | missing_section | unmapped_criterion | other
    target 取 failed_criteria[0]；为空取 files_changed[0] 的目录；再为空取 "round"
    normalize：小写、压空白、路径分隔统一 `/`、剔除 `:\d+` / `#\d+` / 10 位以上数字
    reason_key = sha1(f"{category}|{target}")[:12]
    回避模式固定句式：AVOID[{category}] {target}：上次失败——{error 摘要 ≤80 字}

计数口径：**先取历史计数、再写本次 FailureRecord**，所以 `same_reason_count`
是「本轮历史出现次数（不含本次）」，`escalated = (count + 1) >= 2`（D1-4）。
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from .schemas import (
    ExecResult,
    FailureRecord,
    GateReport,
    GateState,
    TaskPackage,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from .store import Store

#: 同类失败升级阈值（D1-4：同一原因第 2 次失败即 BLOCKED）
ESCALATE_THRESHOLD = 2

#: error_category 白名单（D1-4）；非法值/空值一律归 other
ERROR_CATEGORIES: tuple[str, ...] = (
    "apply_failed",
    "compile",
    "test_fail",
    "missing_section",
    "unmapped_criterion",
    "other",
)

#: 归一化时要剔除的噪声：行号 `:12`、编号 `#7`、时间戳等长数字
_NOISE_RE = re.compile(r"(:\d+|#\d+|\d{10,})")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------- 归一化纯函数


def normalize_category(value: str | None) -> str:
    """error_category 白名单归一化；非法值一律 other（D1-4）。"""
    text = (value or "").strip().lower()
    return text if text in ERROR_CATEGORIES else "other"


def normalize_target(value: str | None) -> str:
    """target 归一化：小写、压空白、`\\` → `/`、剔除行号/编号/长数字。"""
    text = (value or "").strip().lower().replace("\\", "/")
    text = _WS_RE.sub(" ", _NOISE_RE.sub("", text)).strip().strip("/")
    return text or "round"


def make_reason_key(category: str, target: str) -> str:
    """`reason_key = sha1(f"{category}|{target}")[:12]`（D1-4）。"""
    cat = normalize_category(category)
    tgt = normalize_target(target)
    return hashlib.sha1(f"{cat}|{tgt}".encode("utf-8")).hexdigest()[:12]


def target_of(exec_result: ExecResult) -> str:
    """按 D1-4 取 target：failed_criteria[0] → files_changed[0] 的目录 → "round"。"""
    if exec_result.failed_criteria:
        return str(exec_result.failed_criteria[0])
    if exec_result.files_changed:
        first = str(exec_result.files_changed[0]).replace("\\", "/")
        directory = first.rsplit("/", 1)[0] if "/" in first else first
        return directory
    return "round"


def make_avoid_pattern(category: str, target: str, error: str | None) -> str:
    """回避模式固定句式（D1-4），供下一轮任务包 feedback 段注入。"""
    cat = normalize_category(category)
    tgt = normalize_target(target)
    summary = " ".join((error or "").split())[:80] or "（执行器未给原因）"
    return f"AVOID[{cat}] {tgt}：上次失败——{summary}"


def _structural_defects(package: TaskPackage) -> list[str]:
    """G1 的结构缺陷：缺段 + 验收条目缺 cid/text。"""
    defects = list(package.missing_sections())
    for index, criterion in enumerate(package.acceptance, start=1):
        if criterion.missing_fields():
            cid = criterion.cid or f"acceptance[{index}]"
            defects.append(f"{cid}(缺 {'、'.join(criterion.missing_fields())})")
    return defects


def _summary(text: str | None) -> str:
    return " ".join((text or "").split())[:200]


# ---------------------------------------------------------------- 门禁主函数


def evaluate(
    round_no: int,
    package: TaskPackage,
    exec_result: ExecResult,
    store: "Store",
    attempt: int = 1,
) -> GateReport:
    """判定一轮的门禁态；G1–G4 决定失败项，G5 决定 NEEDS_FIX 还是 BLOCKED。"""
    category: str | None = None
    target: str = "round"
    failing: list[str] = []
    reasons: list[str] = []

    defects = _structural_defects(package)
    if defects:  # G1
        category = "missing_section"
        failing = list(defects)
        target = defects[0]
        reasons.append("[gate] G1 任务包缺段/结构缺陷：" + "、".join(defects))
    elif not exec_result.ok:  # G2
        category = normalize_category(exec_result.error_category)
        failing = [str(x) for x in exec_result.failed_criteria]
        target = target_of(exec_result) if not failing else failing[0]
        reasons.append(
            f"[gate] G2 执行器 {exec_result.executor} 报告失败（{category}）："
            f"{_summary(exec_result.error) or '未给原因'}"
        )
    elif exec_result.failed_criteria:  # G3
        category = normalize_category(exec_result.error_category)
        failing = [str(x) for x in exec_result.failed_criteria]
        target = failing[0]
        reasons.append("[gate] G3 执行器声明验收项未通过：" + "、".join(failing))
    else:  # G4
        results = exec_result.results or {}
        unmapped = [c.cid for c in package.acceptance if c.cid not in results]
        if unmapped:
            category = "unmapped_criterion"
            failing = list(unmapped)
            target = unmapped[0]
            reasons.append(
                "[gate] G4 验收断言未映射（执行器 results 未回填）：" + "、".join(unmapped)
            )

    if category is None:  # G6
        return GateReport(
            round_no=round_no,
            state=GateState.PASS,
            reasons=["[gate] G6 全过：结构齐全、执行成功、断言全部映射且通过"],
            failing_criteria=[],
            attempt=attempt,
            escalated=False,
            same_reason_count=0,
            reason_key="",
        )

    reason_key = make_reason_key(category, target)
    # 先取历史计数（不含本次），再落失败记录 —— 口径见模块 docstring
    same_reason_count = store.count_same_failure(round_no, reason_key)
    avoid = make_avoid_pattern(category, target, exec_result.error or "；".join(reasons))
    store.record_failure(
        FailureRecord(
            round_no=round_no,
            attempt=attempt,
            reason="；".join(reasons),
            avoid_pattern=avoid,
            reason_key=reason_key,
        )
    )

    escalated = (same_reason_count + 1) >= ESCALATE_THRESHOLD  # G5
    if escalated:
        reasons.append(
            f"[gate] G5 同类失败第 {same_reason_count + 1} 次（阈值 {ESCALATE_THRESHOLD}）"
            "→ BLOCKED + escalated：停止空转，先做变更影响分析再重规划"
        )
        state = GateState.BLOCKED
    else:
        reasons.append(
            f"[gate] G5 同类失败第 {same_reason_count + 1} 次（阈值 {ESCALATE_THRESHOLD}）"
            "→ NEEDS_FIX：带上回避模式重试一次"
        )
        state = GateState.NEEDS_FIX

    return GateReport(
        round_no=round_no,
        state=state,
        reasons=reasons,
        failing_criteria=failing,
        attempt=attempt,
        escalated=escalated,
        same_reason_count=same_reason_count,
        reason_key=reason_key,
    )


def exhausted_report(
    round_no: int,
    attempt: int,
    store: "Store",
    last: GateReport | None = None,
    reason: str = "attempts_exhausted",
) -> GateReport:
    """重试上限用尽且未触发 G5 时的终态（D1-5：不同原因连续失败到上限 → BLOCKED）。"""
    category = "other"
    target = reason
    reason_key = make_reason_key(category, target)
    same_reason_count = store.count_same_failure(round_no, reason_key)
    detail = "；".join(last.reasons) if last else "无更早的门禁记录"
    text = (
        f"[gate] {reason} 不同原因连续失败到重试上限（第 {attempt} 次）→ BLOCKED："
        f"最后一次原因：{detail}"
    )
    store.record_failure(
        FailureRecord(
            round_no=round_no,
            attempt=attempt,
            reason=text,
            avoid_pattern=make_avoid_pattern(category, target, detail),
            reason_key=reason_key,
        )
    )
    return GateReport(
        round_no=round_no,
        state=GateState.BLOCKED,
        reasons=[text],
        failing_criteria=list(last.failing_criteria) if last else [],
        attempt=attempt,
        escalated=False,
        same_reason_count=same_reason_count,
        reason_key=reason_key,
    )


__all__ = [
    "ESCALATE_THRESHOLD",
    "ERROR_CATEGORIES",
    "normalize_category",
    "normalize_target",
    "make_reason_key",
    "make_avoid_pattern",
    "target_of",
    "evaluate",
    "exhausted_report",
]

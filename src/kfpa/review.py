"""评审位调用与折算（P2，D4 §3.3，契约 1.3）。

要点：
- **fresh context**：每次评审新建会话（不传任何历史 messages），
  `context_id = f"rev-{round_no}-{attempt}-{uuid4 前 8 位}"`；ARCHITECT 侧用 `arch-{round}` 前缀，
  两者不同前缀 + 每次唯一，A19 可断言「与 ARCHITECT 不同、同轮两次不同」。
- **G8 折算**（D3-3.3 冻结，D4 重申）：`major` 且带 `criterion` → 进 `failing_criteria` → NEEDS_FIX；
  其余只记 reasons；**解析失败只记 reasons，不改三态**（R-5 不变）。
  实盘路径上，pipeline 会把发现喂给 `gates.evaluate(reviews=…)`（沿用 P1 的 reason_key/升级口径），
  本模块的 `apply_review_to_gate()` 是同一折算规则的**纯函数**版本，供已有 GateReport 的调用方/测试使用。
- 留痕：`store.add_review_report(report)`（`reviews` 表，磐石提供）；方法缺失时降级为只记日志。

`ReviewReport.provider` / `model_ref` 必须由调用方从 config 传入——本模块不写任何模型串（A5）。
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from .schemas import (
    ExecResult,
    GateReport,
    GateState,
    ReviewFinding,
    ReviewReport,
    Severity,
    TaskPackage,
)

#: 纯任务包评审时用于填充模板的占位（评审只针对任务包，不依赖执行结果）
_EMPTY_EXEC = ExecResult(ok=True)
_EMPTY_GATE = GateReport(round_no=0, state=GateState.PASS)

#: ARCHITECT 侧上下文前缀（A19 要求 reviews.context_id 与之不同）
ARCHITECT_CONTEXT_PREFIX = "arch-"
#: 评审位上下文前缀
REVIEW_CONTEXT_PREFIX = "rev-"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_SEVERITY_ALIASES = {
    "major": Severity.MAJOR,
    "blocker": Severity.MAJOR,
    "high": Severity.MAJOR,
    "严重": Severity.MAJOR,
    "阻塞": Severity.MAJOR,
    "minor": Severity.MINOR,
    "中": Severity.MINOR,
    "info": Severity.INFO,
    "info.": Severity.INFO,
    "低": Severity.INFO,
    "建议": Severity.INFO,
}


class ReviewError(RuntimeError):
    """评审位可判定错误（中文三段式）。"""


def architect_context_id(round_no: int) -> str:
    """ARCHITECT 侧上下文标识（固定前缀 + 轮次，便于 A19 做不等断言）。"""
    return f"{ARCHITECT_CONTEXT_PREFIX}{int(round_no)}"


def new_context_id(round_no: int, attempt: int = 1) -> str:
    """评审位上下文标识：前缀 + 轮次 + 次数 + 随机后缀（每次评审都不同）。"""
    return f"{REVIEW_CONTEXT_PREFIX}{int(round_no)}-{int(attempt)}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- 解析


def _extract_json(raw: str) -> Any:
    text = (raw or "").strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ReviewError(
        "[review] REVIEWER 输出不是可解析的 JSON → 检查 prompts/review.j2 与 provider 剧本（解析失败不改三态）"
    )


def _severity(value: Any) -> Severity:
    return _SEVERITY_ALIASES.get(str(value or "").strip().lower(), Severity.INFO)


def parse_review(raw: str, *, round_no: int, provider: str, model_ref: str,
                 context_id: str, attempt: int = 1) -> ReviewReport:
    """宽容解析评审输出 → `ReviewReport`；失败时 `parse_ok=False` + `verdict="unknown"`。"""
    try:
        data = _extract_json(raw)
    except ReviewError:
        return ReviewReport(
            round_no=int(round_no),
            provider=str(provider),
            model_ref=str(model_ref),
            context_id=str(context_id),
            parse_ok=False,
            verdict="unknown",
            findings=[],
            attempt=int(attempt),
        )

    items: list[Any] = []
    verdict = "unknown"
    if isinstance(data, dict):
        for key in ("findings", "reviews", "items", "data"):
            if isinstance(data.get(key), list):
                items = list(data[key])
                break
        verdict = str(data.get("verdict") or "unknown").strip().lower()
    elif isinstance(data, list):
        items = list(data)

    findings: list[ReviewFinding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        findings.append(
            ReviewFinding(
                severity=_severity(item.get("severity") or item.get("level")),
                location=str(item.get("location") or "").strip(),
                issue=str(item.get("issue") or item.get("suggestion") or "").strip(),
                suggestion=str(item.get("suggestion") or "").strip(),
                criterion=str(item.get("criterion") or "").strip(),
            )
        )
    if verdict not in ("pass", "fail", "unknown"):
        verdict = "fail" if any(f.severity is Severity.MAJOR for f in findings) else "pass"
    if verdict == "unknown" and findings:
        verdict = "fail" if any(f.severity is Severity.MAJOR for f in findings) else "pass"

    return ReviewReport(
        round_no=int(round_no),
        provider=str(provider),
        model_ref=str(model_ref),
        context_id=str(context_id),
        parse_ok=True,
        verdict=verdict,
        findings=findings,
        attempt=int(attempt),
    )


# ---------------------------------------------------------------- 调用（fresh context）


def render_review_prompt(package: TaskPackage) -> str:
    """评审提示词（services 端与测试共用；模板在 prompts/review.j2）。"""
    from .pipeline import build_review_prompt  # 延迟导入，避免循环

    return build_review_prompt(package, _EMPTY_EXEC, _EMPTY_GATE)


def run_review(
    package: TaskPackage,
    llm: Any,
    *,
    provider: str,
    round_no: Optional[int] = None,
    attempt: int = 1,
    model_ref: str = "",
) -> ReviewReport:
    """调 REVIEWER 做一次**全新上下文**评审（不传任何历史消息）。

    `provider` / `model_ref` 由调用方从 config 取（本模块不写模型串）。
    """
    round_no = int(round_no if round_no is not None else package.round_no)
    context_id = new_context_id(round_no, attempt)
    system = render_review_prompt(package)
    user = (
        f"请对第 {round_no} 轮任务包做一次对抗性评审，只输出约定的 JSON；"
        "这是全新会话，你没有此前任何对话历史。"
    )
    raw = llm.complete("REVIEWER", system, user, json_mode=True)
    return parse_review(
        raw,
        round_no=round_no,
        provider=provider,
        model_ref=model_ref,
        context_id=context_id,
        attempt=attempt,
    )


# ---------------------------------------------------------------- G8 折算（纯函数）


def apply_review_to_gate(report: ReviewReport, gate: GateReport) -> GateReport:
    """按 G8 把评审折算进已有门禁结果（纯函数，不改三态以外的语义）。

    - 解析失败 → 只追加 reasons（不改三态，R-5）；
    - `major` 且带 `criterion` → 并入 `failing_criteria`，PASS → NEEDS_FIX（已 BLOCKED 保持 BLOCKED）；
    - 其余（major 无 criterion / minor / info）→ 只追加 reasons。
    """
    reasons = list(gate.reasons or [])
    reasons.append(
        f"[gate] G8 评审（provider={report.provider}，context={report.context_id}，"
        f"parse_ok={report.parse_ok}，verdict={report.verdict}）"
    )
    if not report.parse_ok:
        reasons.append("[gate] G8 评审解析失败 → 只记录，不改三态（R-5）")
        return GateReport(
            round_no=gate.round_no,
            state=gate.state,
            reasons=reasons,
            failing_criteria=list(gate.failing_criteria or []),
            attempt=gate.attempt,
            escalated=gate.escalated,
            same_reason_count=gate.same_reason_count,
            reason_key=gate.reason_key,
        )

    failing = list(gate.failing_criteria or [])
    for finding in report.findings:
        if finding.severity is Severity.MAJOR and finding.criterion.strip():
            if finding.criterion not in failing:
                failing.append(finding.criterion)
            reasons.append(
                f"[gate] G8 评审阻塞（major/{finding.criterion} @ {finding.location}）："
                f"{finding.issue} → {finding.suggestion or '按建议修'}"
            )
        elif finding.severity is Severity.MAJOR:
            reasons.append(
                f"[gate] G8 评审 major 但无 criterion（定位不到断言）@ {finding.location}："
                f"{finding.issue} → 不阻塞"
            )
        else:
            reasons.append(
                f"[gate] G8 评审 {finding.severity.value} @ {finding.location}：{finding.issue}"
            )

    blockers = report.blockers()
    state = gate.state
    if blockers and state is GateState.PASS:
        state = GateState.NEEDS_FIX
    return GateReport(
        round_no=gate.round_no,
        state=state,
        reasons=reasons,
        failing_criteria=failing,
        attempt=gate.attempt,
        escalated=gate.escalated,
        same_reason_count=gate.same_reason_count,
        reason_key=gate.reason_key,
    )


# ---------------------------------------------------------------- 留痕


def persist_report(store: Any, report: ReviewReport) -> bool:
    """把评审写进 `reviews` 表（磐石 P2 提供）；缺方法时降级只记日志，不阻塞主循环。"""
    if store is None or not hasattr(store, "add_review_report"):
        print(
            "[review] store 缺少 add_review_report()（P2 的 reviews 表未就位）→ 本次只记日志",
            file=sys.stderr,
            flush=True,
        )
        return False
    store.add_review_report(report)
    return True


def review_findings_as_dicts(report: ReviewReport) -> list[dict]:
    """转成 `gates.evaluate(reviews=…)` 认的 dict 形状（复用 P1 的 G8 折算与升级口径）。"""
    return [
        {
            "severity": f.severity.value,
            "location": f.location,
            "issue": f.issue,
            "suggestion": f.suggestion,
            "criterion": f.criterion,
        }
        for f in report.findings
    ]


__all__ = [
    "ARCHITECT_CONTEXT_PREFIX",
    "REVIEW_CONTEXT_PREFIX",
    "ReviewError",
    "architect_context_id",
    "new_context_id",
    "parse_review",
    "render_review_prompt",
    "run_review",
    "apply_review_to_gate",
    "persist_report",
    "review_findings_as_dicts",
]

r"""质量门禁三态判定（纯函数，P0 契约 §3.5 + D1-3 / D1-4，P1 增 G7 / G8）。

判定顺序**严格按 D1-3（并含 P1 的 G7 / G8）**，短路返回，**不允许 LLM 参与任何判定**：

| 序 | 条件 | 结果 |
|---|---|---|
| G1 | `package.missing_sections()` 非空（或验收条目缺 cid/text） | NEEDS_FIX，failing_criteria = 缺段名 |
| G2 | `exec_result.ok is False` | NEEDS_FIX，按 `error_category` 定类 |
| G3 | `exec_result.failed_criteria` 非空 | NEEDS_FIX，逐条填 failing_criteria |
| G4 | `acceptance` 中存在 cid 不在 `exec_result.results` 里 | NEEDS_FIX，category=unmapped_criterion |
| G7 | `CoverageReport.passed()` 为假（P1 D3-3.3） | NEEDS_FIX，category=coverage，failing_criteria=未达标文件 |
| G8 | REVIEWER 条目 `severity=major` 且带 `criterion`（P1 D3-3.3） | NEEDS_FIX，`criterion` 进 failing_criteria；其余只记 reasons |
| G5 | `count_same_failure(round, reason_key) + 1 >= 2` | BLOCKED，escalated=True |
| G6 | 以上皆否 | PASS |

失败归一化（D1-4 + P1）：
    category 白名单 apply_failed | compile | test_fail | missing_section | unmapped_criterion | coverage | other
    target 取 failed_criteria[0]；为空取 files_changed[0] 的目录；再为空取 "round"
    normalize：小写、压空白、路径分隔统一 `/`、剔除 `:\d+` / `#\d+` / 10 位以上数字
    reason_key = sha1(f"{category}|{target}")[:12]
    回避模式固定句式：AVOID[{category}] {target}：上次失败——{error 摘要 ≤80 字}

计数口径（D2 R-4）：**先落本次 FailureRecord，再计数（含本次）**，`escalated = count >= 2`，
`GateReport.same_reason_count = count - 1`（保持 D1-4「历史次数、不含本次」的字段语义）。

G8 红线（D3-3.3 + R-5）：REVIEWER 解析失败 / 非 JSON **不改三态**，只记 reasons；
`severity=major` 但没有 `criterion`（定位不到断言）同样**不阻塞**。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .schemas import (
    CoverageReport,
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

#: error_category 白名单（D1-4 + P1 D3-3.3 的 coverage）；非法值/空值一律归 other
ERROR_CATEGORIES: tuple[str, ...] = (
    "apply_failed",
    "compile",
    "test_fail",
    "missing_section",
    "unmapped_criterion",
    "coverage",
    "other",
)

#: G8 会阻塞的严重级（D3-3.3）
BLOCKING_SEVERITIES = ("major", "blocker")

#: 归一化时要剔除的噪声：行号 `:12`、编号 `#7`、时间戳等长数字
_NOISE_RE = re.compile(r"(:\d+|#\d+|\d{10,})")
_WS_RE = re.compile(r"\s+")


class GatesError(RuntimeError):
    """门禁可判定错误（中文、三段式；如覆盖率报告缺失/不可解析）。"""


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


# ---------------------------------------------------------------- G7 覆盖率（P1 D3-3.3）

#: R-22.4 冻结的核心模块白名单（**R-32 修订**：拆出「纯逻辑」与「需外部服务」两类）。
#: 不含 `ui/**`、`executor.py`（适配器）、`scripts/**`；全仓库行覆盖不作为门槛。
CORE_COVERAGE_FILES: tuple[str, ...] = (
    "src/kfpa/schemas.py",
    "src/kfpa/config.py",
    "src/kfpa/cli.py",
    "src/kfpa/gates.py",
    "src/kfpa/pipeline.py",
    "src/kfpa/review.py",
    "src/kfpa/eval.py",
)
#: 白名单目录前缀（`store/**`）
CORE_COVERAGE_PREFIXES: tuple[str, ...] = (
    "src/kfpa/store/",
    "src/kfpa/store",
)
#: R-32：需要外部服务（PG/Docker）的模块**不计入 clean env 的 G7 门槛**，
#: 其覆盖由 A12 双库对拍保证；有 Docker 时由 A20b 单独报告。
CORE_COVERAGE_EXCLUDES: tuple[str, ...] = ("src/kfpa/store/postgres.py",)


def _norm_cov_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")


def in_core_coverage_scope(path: str) -> bool:
    """该文件是否计入 G7 门槛（R-22.4 白名单 + R-32 排除需外部服务的模块）。"""
    normal = _norm_cov_path(path)
    if any(normal == _norm_cov_path(item) for item in CORE_COVERAGE_EXCLUDES):
        return False
    return normal in CORE_COVERAGE_FILES or any(
        normal.startswith(prefix) for prefix in CORE_COVERAGE_PREFIXES
    )


def load_coverage_report(
    json_path: str | Path,
    *,
    critical_paths_ok: Optional[bool] = None,
    scope: Optional[tuple[str, ...]] = None,
) -> CoverageReport:
    """读 `pytest --cov-report=json` 的报告 → `CoverageReport`（G7 的量化入口）。

    - `totals.percent_covered` → `line_rate / 100`；`files[*].summary.percent_covered` → 各文件行覆盖率。
    - **门槛只统计核心模块白名单（R-22.4）**：默认按 `in_core_coverage_scope()` 过滤，
      并用各文件的 `covered_lines / num_statements` 重算 `line_rate`（加权，不是简单平均）；
      报告里一个白名单文件都没有时退回全量口径（避免拿一份无关报告做出「0% 全红」的假判定）。
    - `critical_paths_ok`：显式传入优先；否则读 JSON 里的 `critical_paths_ok` 键；
      两者都没有 → **False（不编造）**，即关键路径未证明全绿时不允许 PASS。
    - 文件不存在 / JSON 解析失败 → 抛 `GatesError`（中文三段式，让调用方决定退出码）。
    """
    path = Path(json_path)
    if not path.is_file():
        raise GatesError(
            f"[gate] 覆盖率报告不存在：{path} → 先跑 `python -m pytest --cov=kfpa --cov-report=json`"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GatesError(
            f"[gate] 覆盖率报告无法解析：{path}（{exc}） → 重新生成 json 报告"
        ) from exc

    totals = payload.get("totals") or {}
    total_rate = float(totals.get("percent_covered", 0.0) or 0.0) / 100.0
    raw_files = payload.get("files") or {}

    all_files: dict[str, float] = {}
    all_statements: dict[str, int] = {}
    all_covered: dict[str, int] = {}
    for name, info in raw_files.items():
        summary = (info or {}).get("summary") or {}
        key = _norm_cov_path(name)
        if "percent_covered" in summary:
            all_files[key] = float(summary["percent_covered"] or 0.0) / 100.0
        if "num_statements" in summary:
            all_statements[key] = int(summary.get("num_statements") or 0)
            all_covered[key] = int(summary.get("covered_lines") or 0)

    scoped = scope if scope is not None else None
    if scoped is None:
        selected = {k: v for k, v in all_files.items() if in_core_coverage_scope(k)}
    else:
        wanted = tuple(_norm_cov_path(s) for s in scoped)
        selected = {
            k: v for k, v in all_files.items() if k in wanted or any(k.startswith(w) for w in wanted)
        }

    if selected:
        statements = sum(all_statements.get(k, 0) for k in selected)
        covered = sum(all_covered.get(k, 0) for k in selected)
        if statements > 0:
            line_rate = covered / statements
        else:  # 报告没给行数时退回各文件覆盖率的算术平均
            line_rate = sum(selected.values()) / len(selected)
        files = selected
    else:  # 报告里没有白名单文件 → 退回全量口径（不编造 0%）
        line_rate, files = total_rate, all_files

    if critical_paths_ok is None:
        critical_paths_ok = bool(payload.get("critical_paths_ok", False))
    return CoverageReport(line_rate=line_rate, files=files, critical_paths_ok=bool(critical_paths_ok))


# ---------------------------------------------------------------- G8 审查折算（P1 D3-3.3）


def parse_reviews(raw: Any) -> list[dict]:
    """宽容解析 REVIEWER 输出（D3-3.3 + R-5）：非 JSON / 结构不符 → 返回空列表，不改三态。

    接受：`{"reviews":[...]}`、裸列表、JSON 字符串（可带 ```json 围栏）、已是列表的 dict。
    """
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        for key in ("reviews", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def fold_reviews(reviews: Any) -> tuple[list[tuple[str, str]], list[str]]:
    """把评审条目折算成 `(阻塞项 (criterion, category), reasons)`（D3-3.3 表）。

    只有 `severity in {major, blocker}` **且**带 `criterion` 的条目才阻塞；
    其余（含无 criterion 的 major、minor/info、解析失败）只记 reasons。
    """
    blocking: list[tuple[str, str]] = []
    reasons: list[str] = []
    items = parse_reviews(reviews)
    if not items:
        if reviews:
            reasons.append("[gate] G8 评审解析失败/无条目 → 只记录，不改三态（R-5）")
        return blocking, reasons
    for index, item in enumerate(items, start=1):
        severity = str(item.get("severity") or item.get("level") or "info").strip().lower()
        criterion = str(item.get("criterion") or "").strip()
        location = str(item.get("location") or "").strip() or f"#{index}"
        issue = _summary(item.get("issue") or item.get("suggestion") or "")
        if severity in BLOCKING_SEVERITIES and criterion:
            category = normalize_category(item.get("error_category"))
            blocking.append((criterion, category))
            reasons.append(
                f"[gate] G8 评审阻塞（{severity}/{criterion} @ {location}）：{issue}"
            )
        elif severity in BLOCKING_SEVERITIES:
            reasons.append(
                f"[gate] G8 评审 {severity} 但无 criterion（定位不到断言）@ {location}：{issue} → 不阻塞"
            )
        else:
            reasons.append(f"[gate] G8 评审 {severity} @ {location}：{issue}")
    return blocking, reasons


# ---------------------------------------------------------------- 门禁主函数


def evaluate(
    round_no: int,
    package: TaskPackage,
    exec_result: ExecResult,
    store: "Store",
    attempt: int = 1,
    *,
    coverage: Optional[CoverageReport] = None,
    reviews: Any = None,
) -> GateReport:
    """判定一轮的门禁态；G1–G4（+P1 的 G7/G8）决定失败项，G5 决定 NEEDS_FIX 还是 BLOCKED。"""
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

    # ---- G7 覆盖率门槛（P1 D3-3.3）：只在 G1–G4 全过时才看
    if category is None and coverage is not None and not coverage.passed():
        category = "coverage"
        fails = coverage.failing_files()
        if not fails:
            fails = ["<总行覆盖率>"]
        if not coverage.critical_paths_ok:
            fails.append("critical_paths")
        failing = list(fails)
        target = failing[0]
        reasons.append(
            f"[gate] G7 覆盖率门槛未过（行 {coverage.line_rate:.2%} < {coverage.threshold_line:.0%}"
            f"，关键路径全绿={coverage.critical_paths_ok}）：" + "、".join(failing)
        )

    # ---- G8 审查折算（P1 D3-3.3）：major + criterion 才阻塞，其余只记 reasons
    if category is None and reviews:
        blocking, review_reasons = fold_reviews(reviews)
        reasons.extend(review_reasons)
        if blocking:
            failing = [cid for cid, _ in blocking]
            target = failing[0]
            category = blocking[0][1]

    if category is None:  # G6
        return GateReport(
            round_no=round_no,
            state=GateState.PASS,
            reasons=reasons or ["[gate] G6 全过：结构齐全、执行成功、断言全部映射且通过"],
            failing_criteria=[],
            attempt=attempt,
            escalated=False,
            same_reason_count=0,
            reason_key="",
        )

    reason_key = make_reason_key(category, target)
    # D2 R-4 固定顺序：先落本次 FailureRecord，再计数（含本次），escalated = count >= 2；
    # GateReport.same_reason_count = count - 1（保持 D1-4「历史次数、不含本次」的字段语义）
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
    same_total = store.count_same_failure(round_no, reason_key)
    same_reason_count = max(0, same_total - 1)

    escalated = same_total >= ESCALATE_THRESHOLD  # G5
    if escalated:
        reasons.append(
            f"[gate] G5 同类失败第 {same_total} 次（阈值 {ESCALATE_THRESHOLD}）"
            "→ BLOCKED + escalated：停止空转，先做变更影响分析再重规划"
        )
        state = GateState.BLOCKED
    else:
        reasons.append(
            f"[gate] G5 同类失败第 {same_total} 次（阈值 {ESCALATE_THRESHOLD}）"
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
    same_total = store.count_same_failure(round_no, reason_key)  # D2 R-4：含本次
    return GateReport(
        round_no=round_no,
        state=GateState.BLOCKED,
        reasons=[text],
        failing_criteria=list(last.failing_criteria) if last else [],
        attempt=attempt,
        escalated=False,
        same_reason_count=max(0, same_total - 1),
        reason_key=reason_key,
    )


__all__ = [
    "ESCALATE_THRESHOLD",
    "ERROR_CATEGORIES",
    "BLOCKING_SEVERITIES",
    "CORE_COVERAGE_FILES",
    "CORE_COVERAGE_PREFIXES",
    "GatesError",
    "in_core_coverage_scope",
    "normalize_category",
    "normalize_target",
    "make_reason_key",
    "make_avoid_pattern",
    "target_of",
    "load_coverage_report",
    "parse_reviews",
    "fold_reviews",
    "evaluate",
    "exhausted_report",
]

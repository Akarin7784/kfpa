"""KFPA 轮次生命周期编排（P0，契约 §3.4 + 设计补充件 D1-5 / D1-10 / D1-12）。

本模块只做**编排与渲染**，不持有模型字符串、不直接读写 SQLite：
- `store` / `llm` / `executor` 全部由调用方（CLI，磐石）按契约注入；
- 提示词模板放在 `src/kfpa/prompts/`，用 Jinja2 渲染（惰性 import，缺包给中文提示）。

业务判定全部来自纯函数（D1-12 约束 1/2）：
`gates.evaluate`（门禁）、`gates.make_reason_key`（归一化）、`package_hash`、六段自检、
`apply_impact_floor`（D1-2 影响面兜底）、`cap_tickets`（D1-1 开票上限）。
`run_round` 是纯函数 + 循环的参考实现；`build_graph` 是同语义的 LangGraph 编排层
（节点只调用这些纯函数 + Store + LLM/Executor，节点内不写判据）。

对外接口：
    generate_tickets(round_no, design_text, llm) -> list[QATicket]
    build_task_package(round_no, design_text, tickets, llm, avoid_patterns) -> TaskPackage
    run_round(round_no, design_text, design_ref, *, store, llm, executor, workdir,
              max_attempts=3, dry_run=True, injection="none") -> RoundResult
    build_graph(store, llm, executor, workdir, *, max_attempts=3, dry_run=True,
                injection="none") -> 已编译的 LangGraph 图
    run_round_via_graph(...) -> RoundResult
    round_exit_code(result) -> int          # D1-5 退出码：0 / 10 / 11 / 3
    render_task_package(package) -> str     # 六段式任务包 Markdown 交付物
    build_review_prompt(package, exec_result, gate) -> str

CLI 退出码（D1-5）：
    0  PASS（并通过快照）
    10 NEEDS_FIX（未耗尽重试上限）
    11 BLOCKED（升级主BOT / attempts_exhausted）
    3  存在未关闭的高影响質問票，拒绝生成任务包

日志：每阶段一行 `[i/6] 阶段名 …` 输出到 **stderr**（保持 stdout 干净，供 `--json` 单行输出）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, TypedDict

from . import gates
from .schemas import (
    AcceptanceCriterion,
    ExecResult,
    GateState,
    QATicket,
    Round,
    RoundResult,
    RoundState,
    Snapshot,
    TaskPackage,
    TicketImpact,
    TicketStatus,
    package_hash,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，运行期不依赖磐石的模块
    from .executor import Executor
    from .llm import LLM
    from .store import Store

#: 提示词模板目录
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# ---------------------------------------------------------------- 退出码（D1-5）

EXIT_PASS = 0
EXIT_NEEDS_FIX = 10
EXIT_BLOCKED = 11
EXIT_OPEN_HIGH_TICKETS = 3

#: 高影响票硬规则的机器可判定标记（CLI 与测试都可据此断言）
BLOCKED_MARK = "[KFPA-BLOCKED]"

#: 单轮开票上限（D1-1：超过则 LOW 合并成一条汇总票）
MAX_TICKETS_PER_ROUND = 20

#: 影响面兜底规则（D1-2）：「至少 HIGH」的关键词组
IMPACT_FLOOR_HIGH = (
    "新增字段",
    "删除字段",
    "新增接口",
    "删除接口",
    "改主键",
    "改唯一键",
    "主键",
    "唯一键",
)
#: 「至少 MEDIUM」的关键词组
IMPACT_FLOOR_MEDIUM = ("字段", "表", "接口", "权限", "金额", "数量")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_QA_LINE_RE = re.compile(
    r"QA:\s*(?P<location>.*?)\s*\|\s*A:\s*(?P<option_a>.*?)\s*\|\s*B:\s*(?P<option_b>.*?)"
    r"\s*\|\s*P:\s*(?P<pref>.*?)\s*\|\s*I:\s*(?P<impact>[A-Za-z\u4e00-\u9fa5]+)",
    re.S,
)

_IMPACT_ALIASES = {
    "high": TicketImpact.HIGH,
    "高": TicketImpact.HIGH,
    "高影响": TicketImpact.HIGH,
    "low": TicketImpact.LOW,
    "低": TicketImpact.LOW,
    "低影响": TicketImpact.LOW,
    "medium": TicketImpact.MEDIUM,
    "中": TicketImpact.MEDIUM,
    "中影响": TicketImpact.MEDIUM,
    "mid": TicketImpact.MEDIUM,
}

#: 票面字段别名（容忍 MockLLM / 真实模型的不同命名）
_FIELD_ALIASES = {
    "location": ("location", "位置", "loc"),
    "option_a": ("option_a", "a", "解释A", "解释a", "optionA"),
    "option_b": ("option_b", "b", "解释B", "解释b", "optionB"),
    "architect_preference": (
        "architect_preference",
        "preference",
        "倾向",
        "主BOT倾向",
        "architectpreference",
    ),
    "impact": ("impact", "影响面", "影响", "level"),
}


class PipelineError(RuntimeError):
    """流水线可判定的业务错误（中文、三段式：模块 + 原因 + 下一步）。"""


class DependencyMissingError(PipelineError):
    """可选/必装依赖缺失（CLI 映射退出码 4）。"""


class OpenHighTicketsError(PipelineError):
    """硬规则（D1-1）：存在未关闭的高影响質問票时，不得生成任务包（CLI 退出码 3）。"""

    def __init__(self, ticket_ids: list[str]) -> None:
        self.ticket_ids = list(ticket_ids)
        joined = "、".join(self.ticket_ids)
        super().__init__(
            f"[pipeline] {BLOCKED_MARK} 存在 {len(self.ticket_ids)} 张未关闭的高影响質問票（{joined}）"
            " → 先攒批确认并 `kfpa tickets close`，再重跑本轮（退出码 3）"
        )


def _log(text: str) -> None:
    """阶段日志：每阶段一行，写 stderr，避免污染 --json 的 stdout。"""
    print(text, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 模板渲染

_ENV: Any = None


def _jinja_env() -> Any:
    global _ENV
    if _ENV is None:
        try:
            import jinja2
        except ImportError as exc:  # pragma: no cover - 依赖由 Chief 统一安装
            raise DependencyMissingError(
                "[pipeline] 缺少 jinja2（模板渲染） → 让 Chief 按 P0 施工图统一安装，不要自建 venv（退出码 4）"
            ) from exc
        _ENV = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            autoescape=False,
        )
    return _ENV


def _render(template_name: str, **context: Any) -> str:
    path = PROMPTS_DIR / template_name
    if not path.is_file():
        raise PipelineError(
            f"[pipeline] 模板缺失：{path} → 检查 src/kfpa/prompts/（长流维护，见 P0 施工图 §2）"
        )
    return _jinja_env().get_template(template_name).render(**context)


# ---------------------------------------------------------------- JSON 解析


def _extract_json(raw: str) -> Any:
    """从模型输出里抠出 JSON（容忍 ```json 围栏与前后解释文字）。"""
    text = (raw or "").strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise PipelineError(
        "[pipeline] ARCHITECT 输出不是可解析的 JSON"
        f"（前 200 字：{text[:200]!r}） → 检查 MockLLM marker 剧本与 prompts/*.j2"
    )


def _first_json_object(raw: str) -> dict:
    data = _extract_json(raw)
    if isinstance(data, dict):
        for key in ("package", "task_package", "data", "result"):
            if isinstance(data.get(key), dict):
                return data[key]
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    raise PipelineError(
        "[pipeline] ARCHITECT 输出不是任务包 JSON 对象 → 检查 prompts/task_package.j2 与 MockLLM 剧本"
    )


def _ticket_items(raw: str) -> list[Any]:
    data = _extract_json(raw)
    if isinstance(data, dict):
        for key in ("tickets", "items", "data", "question_tickets", "qa_tickets"):
            value = data.get(key)
            if isinstance(value, list):
                return list(value)
        return []
    if isinstance(data, list):
        return list(data)
    return []


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            text = (
                str(item).strip()
                if not isinstance(item, dict)
                else str(item.get("text") or item.get("desc") or "").strip()
            )
            if text:
                out.append(text)
        return out
    text = str(value).strip()
    return [text] if text else []


def _parse_impact(value: Any) -> TicketImpact:
    return _IMPACT_ALIASES.get(str(value or "").strip().lower(), TicketImpact.MEDIUM)


def _pick(item: dict, field: str) -> str:
    for alias in _FIELD_ALIASES[field]:
        if alias in item and item[alias] is not None:
            return str(item[alias]).strip()
    return ""


# ---------------------------------------------------------------- 票面（D1-1 / D1-2）


def apply_impact_floor(tickets: list[QATicket]) -> list[QATicket]:
    """影响面兜底（D1-2 第二道闸）：防「该 HIGH 的票被降级」。"""
    for ticket in tickets:
        text = " ".join(
            [
                str(ticket.location or ""),
                str(ticket.option_a or ""),
                str(ticket.option_b or ""),
                str(ticket.architect_preference or ""),
            ]
        )
        if any(keyword in text for keyword in IMPACT_FLOOR_HIGH):
            ticket.impact = TicketImpact.HIGH
        elif any(keyword in text for keyword in IMPACT_FLOOR_MEDIUM) and ticket.impact is TicketImpact.LOW:
            ticket.impact = TicketImpact.MEDIUM
    return tickets


def cap_tickets(round_no: int, tickets: list[QATicket]) -> list[QATicket]:
    """开票上限（D1-1）：单轮 > 20 张时只保留 HIGH/MEDIUM，LOW 合并成一条汇总票。"""
    if len(tickets) <= MAX_TICKETS_PER_ROUND:
        return tickets
    kept = [t for t in tickets if t.impact is not TicketImpact.LOW]
    lows = [t for t in tickets if t.impact is TicketImpact.LOW]
    if lows:
        kept.append(
            QATicket(
                ticket_id="",
                round_no=round_no,
                location="（LOW 票汇总）",
                option_a=f"{len(lows)} 条低影响措辞问题合并上呈",
                option_b="逐条单独确认（超出单轮 20 张上限）",
                architect_preference="按上游原有顺序逐条列出，本条仅占位，不掩盖任何一条",
                impact=TicketImpact.LOW,
            )
        )
    return kept


def _ticket_from_item(round_no: int, item: Any) -> Optional[QATicket]:
    """把 mock/模型返回的一条记录转成 QATicket（票号稍后统一分配）。"""
    if isinstance(item, str):
        match = _QA_LINE_RE.search(item)
        if not match:
            return None
        return QATicket(
            ticket_id="",
            round_no=round_no,
            location=match.group("location").strip(),
            option_a=match.group("option_a").strip(),
            option_b=match.group("option_b").strip(),
            architect_preference=match.group("pref").strip(),
            impact=_parse_impact(match.group("impact")),
        )
    if not isinstance(item, dict):
        return None
    return QATicket(
        ticket_id="",
        round_no=round_no,
        location=_pick(item, "location"),
        option_a=_pick(item, "option_a"),
        option_b=_pick(item, "option_b"),
        architect_preference=_pick(item, "architect_preference"),
        impact=_parse_impact(_pick(item, "impact")),
    )


def generate_tickets(round_no: int, design_text: str, llm: "LLM") -> list[QATicket]:
    """设计文本 → 質問票清单（ARCHITECT 位，转译即提问）。

    票号由本模块分配（`R{round}-{seq:03d}`，D1-1），模型/mock 都不许自己编号；
    之后依次过 D1-2 影响面兜底与 D1-1 开票上限。
    """
    system = _render("qa_tickets.j2", round_no=round_no)
    raw = llm.complete("ARCHITECT", system, design_text, json_mode=True)
    parsed: list[QATicket] = []
    for item in _ticket_items(raw):
        ticket = _ticket_from_item(round_no, item)
        if ticket is not None:
            parsed.append(ticket)

    parsed = cap_tickets(round_no, apply_impact_floor(parsed))
    for seq, ticket in enumerate(parsed, start=1):
        ticket.ticket_id = f"R{round_no}-{seq:03d}"
    return parsed


# ---------------------------------------------------------------- 任务包


def _package_user_prompt(design_text: str, tickets: list[QATicket]) -> str:
    lines: list[str] = ["## 原始设计文本", design_text.strip(), "", "## 質問票与人工确认结论"]
    if not tickets:
        lines.append("- （本轮无質問票）")
    for ticket in tickets:
        answer = (ticket.answer or "").strip() or "（未确认）"
        lines.append(
            f"- {ticket.ticket_id} [{ticket.impact.value}] 位置：{ticket.location}；"
            f"解释A：{ticket.option_a}；解释B：{ticket.option_b}；"
            f"主BOT倾向：{ticket.architect_preference}；状态：{ticket.status.value}；人工确认：{answer}"
        )
    return "\n".join(lines)


def _coerce_acceptance(value: Any) -> list[AcceptanceCriterion]:
    criteria: list[AcceptanceCriterion] = []
    if isinstance(value, dict):
        items: list[Any] = [{"cid": k, "text": v} for k, v in value.items()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        items = [line for line in value.splitlines() if line.strip()]
    else:
        items = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, dict):
            cid = str(item.get("cid") or item.get("id") or "").strip() or f"C{index}"
            text = str(item.get("text") or item.get("desc") or item.get("assertion") or "").strip()
        else:
            cid, text = f"C{index}", str(item).strip()
        criteria.append(AcceptanceCriterion(cid=cid, text=text))
    return criteria


def build_task_package(
    round_no: int,
    design_text: str,
    tickets: list[QATicket],
    llm: "LLM",
    avoid_patterns: list[str],
) -> TaskPackage:
    """質問票 + 设计 → 六段式任务包。

    硬规则（D1-1）：存在未关闭的高影响票时**必须拒绝**，抛 OpenHighTicketsError（CLI 退出码 3）。
    """
    blockers = [t.ticket_id for t in tickets if t.is_open_high()]
    if blockers:
        raise OpenHighTicketsError(blockers)

    closed = [t.ticket_id for t in tickets if t.status is TicketStatus.CLOSED]
    avoid = [str(p).strip() for p in (avoid_patterns or []) if str(p).strip()]
    system = _render(
        "task_package.j2",
        round_no=round_no,
        closed_tickets=closed,
        avoid_patterns=avoid,
    )
    raw = llm.complete(
        "ARCHITECT", system, _package_user_prompt(design_text, tickets), json_mode=True
    )
    data = _first_json_object(raw)

    feature = str(data.get("feature") or "").strip() or f"round-{round_no}"
    prohibitions = _as_str_list(data.get("prohibitions"))
    # 回灌区（D1-11）：pipeline 兜底注入 AVOID[...] 行，保证「失败必回灌」不靠模型自觉
    lines = [str(p).strip() for p in data.get("feedback") if str(p).strip()] if isinstance(
        data.get("feedback"), list
    ) else []
    for pattern in avoid:
        if pattern not in lines:
            lines.append(pattern)
        prohibitions.append(f"规避历史失败：{pattern}")

    return TaskPackage(
        round_no=round_no,
        feature=feature,
        goal=str(data.get("goal") or "").strip(),
        acceptance=_coerce_acceptance(data.get("acceptance")),
        boundaries=_as_str_list(data.get("boundaries")),
        interface_contract=_as_str_list(data.get("interface_contract")),
        prohibitions=prohibitions,
        feedback=lines,
        closed_tickets=closed,
    )


def render_task_package(package: TaskPackage) -> str:
    """把任务包渲染成六段式 Markdown 交付物（含 package_hash 指纹）。"""
    return _render("task_package.md.j2", package=package, package_hash=package_hash(package))


def build_review_prompt(package: TaskPackage, exec_result: ExecResult, gate: Any) -> str:
    """评审位（REVIEWER）提示词：强制全新上下文的对抗性评审。"""
    return _render(
        "review.j2",
        package=package,
        package_md=render_task_package(package),
        exec_result=exec_result,
        gate=gate,
    )


# ---------------------------------------------------------------- 退出码


def round_exit_code(result: RoundResult) -> int:
    """把一轮结果映射成 D1-5 的进程退出码。"""
    if result.state is RoundState.SNAPSHOTTED:
        return EXIT_PASS
    if result.state is RoundState.TICKETS_OPEN:
        return EXIT_OPEN_HIGH_TICKETS
    if result.state is RoundState.BLOCKED:
        return EXIT_BLOCKED
    return EXIT_NEEDS_FIX


# ---------------------------------------------------------------- 基础设施


def _git_sha(workdir: Path) -> str:
    """尽力取当前提交号；拿不到不算失败（P0 灰度：回滚到提示即可）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_tag(workdir: Path, tag: str, message: str) -> bool:
    """打 annotated tag（D1-6：只有 PASS 轮）。失败不算流水线失败，只记 False。"""
    try:
        proc = subprocess.run(
            ["git", "tag", "-f", "-a", tag, "-m", message],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _save_round_tickets(store: "Store", tickets: list[QATicket]) -> list[QATicket]:
    """落库質問票：**已存在的票不覆盖**（人工确认结论不能被重跑冲掉，D1-8 幂等）。"""
    saved: list[QATicket] = []
    for ticket in tickets:
        existing: Optional[QATicket] = None
        if hasattr(store, "get_ticket"):
            existing = store.get_ticket(ticket.ticket_id)
        saved.append(existing if existing is not None else store.add_ticket(ticket))
    return saved


# ---------------------------------------------------------------- 阶段（图与循环共用）


def _stage_tickets(store: "Store", llm: "LLM", round_no: int, design_text: str) -> list[QATicket]:
    _log(f"[2/6] 質問票转译：ARCHITECT 转译设计（marker [KFPA:GEN_TICKETS]）")
    fresh = generate_tickets(round_no, design_text, llm)
    _save_round_tickets(store, fresh)
    all_tickets = store.list_tickets(round_no=round_no)
    store.set_round_state(round_no, RoundState.TICKETS_OPEN)
    _log(f"[2/6] 質問票转译完成：本轮共 {len(all_tickets)} 张（新开 {len(fresh)} 张）")
    return all_tickets


def _stage_package(
    store: "Store",
    llm: "LLM",
    round_no: int,
    design_text: str,
    tickets: list[QATicket],
) -> TaskPackage:
    avoid = store.list_avoid_patterns()
    _log(f"[4/6] 任务包生成：六段式，回灌区注入 {len(avoid)} 条历史失败模式")
    package = build_task_package(round_no, design_text, tickets, llm, avoid)
    store.save_package(package)
    store.set_round_state(round_no, RoundState.RELEASED)
    return package


def _stage_execute(
    store: "Store",
    executor: "Executor",
    package: TaskPackage,
    workdir: Path,
    attempt: int,
) -> ExecResult:
    _log(f"[5/6] 执行（第 {attempt} 次尝试）：executor={getattr(executor, 'name', 'unknown')}")
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "package.md").write_text(render_task_package(package), encoding="utf-8")
    store.set_round_state(package.round_no, RoundState.EXECUTING)
    result = executor.apply(package, workdir)
    store.set_round_state(package.round_no, RoundState.GATED)
    return result


def _stage_gate(
    store: "Store",
    package: TaskPackage,
    exec_result: ExecResult,
    attempt: int,
) -> Any:
    report = gates.evaluate(package.round_no, package, exec_result, store, attempt=attempt)
    store.save_gate_report(report)
    _log(
        f"[6/6] 门禁判定（第 {attempt} 次尝试）：{report.state.value}"
        f"｜failing={','.join(report.failing_criteria) or '-'}"
        f"｜reason_key={report.reason_key or '-'}"
    )
    return report


def _stage_snapshot(
    store: "Store",
    package: TaskPackage,
    report: Any,
    workdir: Path,
    round_no: int,
    dry_run: bool,
) -> Snapshot:
    tag = f"kfpa-r{round_no}"
    message = f"package_hash={package_hash(package)} gate_state={report.state.value}"
    if not dry_run:
        _git_tag(workdir, tag, message)
    snapshot = Snapshot(
        round_no=round_no,
        package_hash=package_hash(package),
        gate_state=report.state,
        git_sha=_git_sha(workdir),
        tag=tag,
    )
    store.save_snapshot(snapshot)
    store.set_round_state(round_no, RoundState.SNAPSHOTTED)
    _log(f"[snapshot] 第 {round_no} 轮已快照：tag={tag} package_hash={snapshot.package_hash}")
    return snapshot


# ---------------------------------------------------------------- 轮次编排（参考实现）


def run_round(
    round_no: int,
    design_text: str,
    design_ref: str,
    *,
    store: "Store",
    llm: "LLM",
    executor: "Executor",
    workdir: str | Path,
    max_attempts: int = 3,
    dry_run: bool = True,
    injection: str = "none",
) -> RoundResult:
    """跑一轮完整生命周期：设计录入→开票→放行→任务包→执行→门禁→快照。

    - 高影响票未清零时**不进入执行**，返回 state=TICKETS_OPEN + BLOCKED_MARK（退出码 3）；
    - `injection` 非 none 时透传给支持注入的执行器（D1-7，供 A4 验收）；
    - 不同原因连续失败到 `max_attempts` → BLOCKED + reason=attempts_exhausted（D1-5）。
    """
    workdir_path = Path(workdir)
    workdir_path.mkdir(parents=True, exist_ok=True)
    store.init_schema()
    _log(f"[1/6] 设计录入：ref={design_ref} round={round_no} dry_run={dry_run}")
    store.upsert_round(Round(round_no=round_no, design_ref=design_ref, state=RoundState.DESIGN_IN))

    if injection and injection != "none" and hasattr(executor, "injection"):
        executor.injection = injection  # type: ignore[attr-defined]

    tickets = _stage_tickets(store, llm, round_no, design_text)

    # [3/6] 硬规则闸（D1-1）
    open_high = store.list_open_high_tickets(round_no)
    if open_high:
        ids = [t.ticket_id for t in open_high]
        _log(f"[3/6] 硬规则闸：拦停，未关闭高影响票 {len(ids)} 张（{'、'.join(ids)}）")
        return RoundResult(
            round_no=round_no,
            state=RoundState.TICKETS_OPEN,
            tickets=tickets,
            message=(
                f"{BLOCKED_MARK} [pipeline] 第 {round_no} 轮拦停：{len(ids)} 张未关闭高影响票"
                f"（{'、'.join(ids)}） → 先 `kfpa tickets close` 攒批确认后重跑本轮（退出码 3）"
            ),
        )
    _log("[3/6] 硬规则闸：通过（无未关闭高影响票）")

    package = _stage_package(store, llm, round_no, design_text, tickets)

    last_exec: Optional[ExecResult] = None
    last_gate: Any = None
    attempt = 0

    while attempt < max(1, max_attempts):
        attempt += 1
        exec_result = _stage_execute(store, executor, package, workdir_path, attempt)
        report = _stage_gate(store, package, exec_result, attempt)
        last_exec, last_gate = exec_result, report

        if report.state is GateState.PASS:
            snapshot = _stage_snapshot(store, package, report, workdir_path, round_no, dry_run)
            (workdir_path / "review-prompt.md").write_text(
                build_review_prompt(package, exec_result, report), encoding="utf-8"
            )
            return RoundResult(
                round_no=round_no,
                state=RoundState.SNAPSHOTTED,
                tickets=tickets,
                task_package=package,
                exec_result=exec_result,
                gate=report,
                snapshot=snapshot,
                message=(
                    f"[pipeline] 第 {round_no} 轮门禁 PASS（第 {attempt} 次尝试）："
                    f"package_hash={snapshot.package_hash}，已快照 tag={snapshot.tag}，"
                    f"验收断言 {len(report.failing_criteria) or len(package.acceptance)} 条全过"
                ),
            )

        # 失败必回灌（D1-11）：把 AVOID 行写进任务包回灌区并落库
        avoid = store.list_avoid_patterns()
        package.feedback = list(avoid) or [
            f"第 {attempt} 次尝试失败：{'；'.join(report.reasons)}"
        ]
        store.save_package(package)

        if report.state is GateState.BLOCKED:
            store.set_round_state(round_no, RoundState.BLOCKED)
            message = (
                f"{BLOCKED_MARK} [gate] 第 {round_no} 轮 BLOCKED（第 {attempt} 次尝试）："
                f"同类失败第 {report.same_reason_count + 1} 次（阈值 {gates.ESCALATE_THRESHOLD}）"
                f" → 先做变更影响分析再重规划（退出码 11）"
            )
            _log(message)
            return RoundResult(
                round_no=round_no,
                state=RoundState.BLOCKED,
                tickets=tickets,
                task_package=package,
                exec_result=exec_result,
                gate=report,
                message=message,
            )

        if attempt >= max(1, max_attempts):
            exhausted = gates.exhausted_report(round_no, attempt, store, last=report)
            store.save_gate_report(exhausted)
            store.set_round_state(round_no, RoundState.BLOCKED)
            message = (
                f"{BLOCKED_MARK} [gate] 第 {round_no} 轮 BLOCKED：不同原因连续失败到重试上限"
                f"（attempts_exhausted，{attempt} 次） → 先做变更影响分析再重规划（退出码 11）"
            )
            _log(message)
            return RoundResult(
                round_no=round_no,
                state=RoundState.BLOCKED,
                tickets=tickets,
                task_package=package,
                exec_result=exec_result,
                gate=exhausted,
                message=message,
            )

        package = _stage_package(store, llm, round_no, design_text, tickets)

    raise PipelineError(
        "[pipeline] 内部错误：尝试循环未产出结果 → 检查 max_attempts 与门禁实现"
    )  # pragma: no cover


# ---------------------------------------------------------------- LangGraph 编排层（D1-12）


def build_graph(
    store: "Store",
    llm: "LLM",
    executor: "Executor",
    workdir: str | Path = "runs",
    *,
    max_attempts: int = 3,
    dry_run: bool = True,
    injection: str = "none",
) -> Any:
    """编译 LangGraph `StateGraph`（P0 编排层，D1-12）。

    节点只做「调用纯函数 + 落库 + 调执行器」，判据一律来自 `gates` / 本模块纯函数。
    checkpoint 用 `InMemorySaver`（P0 单进程）；台账真相源仍是 SQLite（D1-9）。

    用法：:

        app = build_graph(store, llm, executor, workdir=tmp)
        state = app.invoke({"round_no": 1, "design_text": text, "design_ref": "examples/design-r1.md"},
                           {"configurable": {"thread_id": "round-1"}})
        state["result"]      # RoundResult

    也可直接用 `run_round_via_graph(...)`（等价于 run_round，只多走一层图）。
    """
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover - 依赖由 Chief 统一安装
        raise DependencyMissingError(
            "[pipeline] 缺少 langgraph（编排层） → 让 Chief 按 D1-12 统一安装，"
            "或用 run_round 纯函数路径（退出码 4）"
        ) from exc

    workdir_path = Path(workdir)
    if injection and injection != "none" and hasattr(executor, "injection"):
        executor.injection = injection  # type: ignore[attr-defined]

    class RoundGraphState(TypedDict, total=False):
        """图状态：节点间只传数据，判据一律来自纯函数（D1-12 约束 2）。"""

        round_no: int
        design_text: str
        design_ref: str
        tickets: list[QATicket]
        package: TaskPackage
        exec_result: ExecResult
        gate: Any
        attempt: int
        result: RoundResult

    def node_round_start(state: dict) -> dict:
        round_no = int(state["round_no"])
        workdir_path.mkdir(parents=True, exist_ok=True)
        store.init_schema()
        _log(f"[1/6] 设计录入：ref={state.get('design_ref', '')} round={round_no} dry_run={dry_run}")
        store.upsert_round(
            Round(
                round_no=round_no,
                design_ref=str(state.get("design_ref", "")),
                state=RoundState.DESIGN_IN,
            )
        )
        return {}

    def node_tickets(state: dict) -> dict:
        tickets = _stage_tickets(store, llm, int(state["round_no"]), str(state["design_text"]))
        return {"tickets": tickets}

    def route_hard_rule(state: dict) -> str:
        return "blocked" if store.list_open_high_tickets(int(state["round_no"])) else "package"

    def node_open_high(state: dict) -> dict:
        round_no = int(state["round_no"])
        ids = [t.ticket_id for t in store.list_open_high_tickets(round_no)]
        _log(f"[3/6] 硬规则闸：拦停，未关闭高影响票 {len(ids)} 张（{'、'.join(ids)}）")
        result = RoundResult(
            round_no=round_no,
            state=RoundState.TICKETS_OPEN,
            tickets=state.get("tickets") or [],
            message=(
                f"{BLOCKED_MARK} [pipeline] 第 {round_no} 轮拦停：{len(ids)} 张未关闭高影响票"
                f"（{'、'.join(ids)}） → 先 `kfpa tickets close` 攒批确认后重跑本轮（退出码 3）"
            ),
        )
        return {"result": result}

    def node_package(state: dict) -> dict:
        round_no = int(state["round_no"])
        _log("[3/6] 硬规则闸：通过（无未关闭高影响票）")
        package = _stage_package(
            store,
            llm,
            round_no,
            str(state["design_text"]),
            state.get("tickets") or [],
        )
        return {"package": package}

    def node_execute(state: dict) -> dict:
        attempt = int(state.get("attempt", 0)) + 1
        exec_result = _stage_execute(
            store, executor, state["package"], workdir_path, attempt
        )
        return {"attempt": attempt, "exec_result": exec_result}

    def node_gate(state: dict) -> dict:
        report = _stage_gate(
            store, state["package"], state["exec_result"], int(state.get("attempt", 1))
        )
        return {"gate": report}

    def route_gate(state: dict) -> str:
        report = state["gate"]
        if report.state is GateState.PASS:
            return "snapshot"
        if report.state is GateState.BLOCKED:
            return "blocked"
        return "feedback" if int(state.get("attempt", 1)) < max(1, max_attempts) else "exhausted"

    def node_feedback(state: dict) -> dict:
        """失败必回灌（D1-11）：带着 AVOID 行重建任务包，再执行一次。"""
        round_no = int(state["round_no"])
        package = _stage_package(
            store, llm, round_no, str(state["design_text"]), state.get("tickets") or []
        )
        return {"package": package}

    def node_snapshot(state: dict) -> dict:
        package = state["package"]
        report = state["gate"]
        snapshot = _stage_snapshot(
            store, package, report, workdir_path, int(state["round_no"]), dry_run
        )
        (workdir_path / "review-prompt.md").write_text(
            build_review_prompt(package, state["exec_result"], report), encoding="utf-8"
        )
        result = RoundResult(
            round_no=int(state["round_no"]),
            state=RoundState.SNAPSHOTTED,
            tickets=state.get("tickets") or [],
            task_package=package,
            exec_result=state["exec_result"],
            gate=report,
            snapshot=snapshot,
            message=(
                f"[pipeline] 第 {int(state['round_no'])} 轮门禁 PASS（第 {state.get('attempt')} 次尝试）："
                f"package_hash={snapshot.package_hash}，已快照 tag={snapshot.tag}"
            ),
        )
        return {"result": result}

    def node_blocked(state: dict) -> dict:
        round_no = int(state["round_no"])
        report = state["gate"]
        package = state["package"]
        store.set_round_state(round_no, RoundState.BLOCKED)
        message = (
            f"{BLOCKED_MARK} [gate] 第 {round_no} 轮 BLOCKED（第 {state.get('attempt')} 次尝试）："
            f"同类失败第 {report.same_reason_count + 1} 次（阈值 {gates.ESCALATE_THRESHOLD}）"
            " → 先做变更影响分析再重规划（退出码 11）"
        )
        _log(message)
        return {
            "result": RoundResult(
                round_no=round_no,
                state=RoundState.BLOCKED,
                tickets=state.get("tickets") or [],
                task_package=package,
                exec_result=state.get("exec_result"),
                gate=report,
                message=message,
            )
        }

    def node_exhausted(state: dict) -> dict:
        round_no = int(state["round_no"])
        attempt = int(state.get("attempt", 1))
        package = state.get("package")
        if package is not None:  # 终止前仍把回灌区补齐（失败必回灌）
            package.feedback = list(store.list_avoid_patterns()) or [
                f"第 {attempt} 次尝试失败：{'；'.join(state['gate'].reasons)}"
            ]
            store.save_package(package)
        report = gates.exhausted_report(round_no, attempt, store, last=state.get("gate"))
        store.save_gate_report(report)
        store.set_round_state(round_no, RoundState.BLOCKED)
        message = (
            f"{BLOCKED_MARK} [gate] 第 {round_no} 轮 BLOCKED：不同原因连续失败到重试上限"
            f"（attempts_exhausted，{attempt} 次） → 先做变更影响分析再重规划（退出码 11）"
        )
        _log(message)
        return {
            "result": RoundResult(
                round_no=round_no,
                state=RoundState.BLOCKED,
                tickets=state.get("tickets") or [],
                task_package=state.get("package"),
                exec_result=state.get("exec_result"),
                gate=report,
                message=message,
            )
        }

    graph = StateGraph(RoundGraphState)
    graph.add_node("round_start", node_round_start)
    graph.add_node("tickets", node_tickets)
    graph.add_node("open_high", node_open_high)
    graph.add_node("package", node_package)
    graph.add_node("execute", node_execute)
    graph.add_node("gate", node_gate)
    graph.add_node("feedback", node_feedback)
    graph.add_node("snapshot", node_snapshot)
    graph.add_node("blocked", node_blocked)
    graph.add_node("exhausted", node_exhausted)

    graph.set_entry_point("round_start")
    graph.add_edge("round_start", "tickets")
    graph.add_conditional_edges(
        "tickets", route_hard_rule, {"blocked": "open_high", "package": "package"}
    )
    graph.add_edge("open_high", END)
    graph.add_edge("package", "execute")
    graph.add_edge("execute", "gate")
    graph.add_conditional_edges(
        "gate",
        route_gate,
        {
            "snapshot": "snapshot",
            "blocked": "blocked",
            "feedback": "feedback",
            "exhausted": "exhausted",
        },
    )
    graph.add_edge("feedback", "execute")
    graph.add_edge("snapshot", END)
    graph.add_edge("blocked", END)
    graph.add_edge("exhausted", END)
    return graph.compile(checkpointer=InMemorySaver())


def run_round_via_graph(
    round_no: int,
    design_text: str,
    design_ref: str,
    *,
    store: "Store",
    llm: "LLM",
    executor: "Executor",
    workdir: str | Path,
    max_attempts: int = 3,
    dry_run: bool = True,
    injection: str = "none",
) -> RoundResult:
    """走 LangGraph 编排层跑一轮（A8 冒烟用；语义与 run_round 一致）。"""
    app = build_graph(
        store,
        llm,
        executor,
        workdir,
        max_attempts=max_attempts,
        dry_run=dry_run,
        injection=injection,
    )
    final = app.invoke(
        {"round_no": round_no, "design_text": design_text, "design_ref": design_ref, "attempt": 0},
        {"configurable": {"thread_id": f"kfpa-round-{round_no}"}},
    )
    result = final.get("result") if isinstance(final, dict) else None
    if not isinstance(result, RoundResult):
        raise PipelineError(
            "[pipeline] LangGraph 未产出 RoundResult → 检查图的终止节点与 state['result']"
        )
    return result


__all__ = [
    "PROMPTS_DIR",
    "EXIT_PASS",
    "EXIT_NEEDS_FIX",
    "EXIT_BLOCKED",
    "EXIT_OPEN_HIGH_TICKETS",
    "BLOCKED_MARK",
    "MAX_TICKETS_PER_ROUND",
    "IMPACT_FLOOR_HIGH",
    "IMPACT_FLOOR_MEDIUM",
    "PipelineError",
    "DependencyMissingError",
    "OpenHighTicketsError",
    "apply_impact_floor",
    "cap_tickets",
    "generate_tickets",
    "build_task_package",
    "render_task_package",
    "build_review_prompt",
    "round_exit_code",
    "run_round",
    "build_graph",
    "run_round_via_graph",
]

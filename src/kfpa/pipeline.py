"""KFPA 轮次生命周期编排（P0，契约 §3.4 + D1-12 / D2 R-13 / R-14）。

本模块只做**编排与渲染**，不持有模型字符串、不直接读写 SQLite：
- `store` / `llm` / `executor` 全部由调用方（CLI，磐石）按契约注入；
- 提示词模板放在 `src/kfpa/prompts/`，用 Jinja2 渲染（惰性 import，缺包给中文提示）。

编排按 **D1-12 / R-14**：`build_graph()` 是 LangGraph `StateGraph`，节点只做
「调用纯函数 + 落库 + 调执行器」；所有业务判据来自纯函数
（`gates.evaluate` / `make_reason_key` / `package_hash` / 六段自检 /
`apply_impact_floor`（D1-2）/ `cap_tickets`（D1-1））。
`run_round()` 是 `build_graph(...).invoke(...)` 的**薄包装**，只负责把终止态组装成 `RoundResult`。

冻结的图契约（D2 R-14）::

    class RoundGraphState(TypedDict, total=False):
        round_no / design_text / design_ref            # invoke 时给全
        state / tickets / open_high / task_package / exec_result / gate
        snapshot / attempt / avoid_patterns / message  # 图内填充的终止态

    GRAPH_NODES = ("gen_tickets", "guard_tickets", "build_package", "execute", "gate", "finalize")

    build_graph(*, store, llm, executor, workdir=".", max_attempts=3,
                dry_run=True, injection="none", checkpointer=None)

放行判据按 **R-13**：只有未关闭的 HIGH 票阻塞出包（`rounds.state → TICKETS_OPEN`），
MEDIUM / LOW 未关闭不阻塞（走攒批确认），放行时 `rounds.state → RELEASED`。

退出码：**由 `src/kfpa/cli.py`（磐石）统一映射**（R-1）；本模块只保证 `RoundResult.state` 语义正确。

`injection` 口径（R-10）：`high_ticket` 由 `build_llm(...)` 在 mock 剧本里消化，
**不透传执行器**；只有 `apply_fail` / `test_fail` 才转给 `MockExecutor`。

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
from . import knowledge
from . import ledger as ledger_mod
from . import review as review_mod
from .schemas import (
    AcceptanceCriterion,
    ExecResult,
    GateReport,
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

#: P1 默认 checkpoint 文件名（R-22.2：库固定在 `{workdir}/checkpoints.sqlite`）
CHECKPOINT_DB_NAME = "checkpoints.sqlite"

#: 高影响票硬规则的机器可判定标记（CLI 与测试都可据此断言）
BLOCKED_MARK = "[KFPA-BLOCKED]"

#: 透传执行器的注入档位（R-10：high_ticket 归 mock 剧本，不进执行器）
EXECUTOR_INJECTIONS = ("apply_fail", "test_fail")

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


class RoundGraphState(TypedDict, total=False):
    """LangGraph 状态契约（D2 R-14 冻结）。

    入参三键由调用方给全；其余键由图内节点逐步填充；终止态必须含
    `state / tickets / task_package / exec_result / gate / snapshot / message`。
    """

    # ---- 入参
    round_no: int
    design_text: str
    design_ref: str
    # ---- 图内填充
    state: RoundState
    tickets: list[QATicket]
    open_high: list[str]
    task_package: Optional[TaskPackage]
    exec_result: Optional[ExecResult]
    gate: Optional[GateReport]
    snapshot: Optional[Snapshot]
    attempt: int
    avoid_patterns: list[str]
    message: str


#: 冻结点名（D2 R-14），顺序即执行顺序
GRAPH_NODES = ("gen_tickets", "guard_tickets", "build_package", "execute", "gate", "finalize")

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

#: 票面字段别名（容忍 MockLLM / 真实模型的不同命名；R-3：mock 带 ticket_id 也由本模块覆盖）
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
    """硬规则（D1-1 / R-13）：存在未关闭的高影响質問票时，不得生成任务包（CLI 退出码 3）。"""

    def __init__(self, ticket_ids: list[str]) -> None:
        self.ticket_ids = list(ticket_ids)
        joined = "、".join(self.ticket_ids)
        super().__init__(
            f"[pipeline] {BLOCKED_MARK} 存在 {len(self.ticket_ids)} 张未关闭的高影响質問票（{joined}）"
            " → 先攒批确认并 `kfpa tickets close`，再重跑本轮（退出码 3）"
        )


def _log(text: str) -> None:
    """阶段日志：每阶段一行，写 stderr，避免污染 --json 的 stdout。

    R-18 兜底：控制台/重定向为非 UTF-8（如 cp1252）且未设 `PYTHONIOENCODING` 时，
    中文日志不得让流水线崩掉 —— 编码失败就降级成 ASCII 安全文本再试一次。
    CLI 入口的 `sys.stdout/stderr.reconfigure(utf-8)`（磐石）是主兜底，这里是第二层。
    """
    try:
        print(text, file=sys.stderr, flush=True)
        return
    except UnicodeEncodeError:
        pass
    except (ValueError, OSError):  # pragma: no cover - 流已关闭等
        return
    try:
        print(text.encode("ascii", "replace").decode("ascii"), file=sys.stderr, flush=True)
    except (UnicodeEncodeError, ValueError, OSError):  # pragma: no cover
        pass


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


# ---------------------------------------------------------------- 票面（D1-1 / D1-2 / R-13）


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
    """把 mock/模型返回的一条记录转成 QATicket（票号稍后统一分配，忽略 mock 的 ticket_id）。"""
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
    raw = dict(item)
    raw.pop("ticket_id", None)  # R-3：票号由本模块分配，mock 的 T-001 一律忽略
    return QATicket(
        ticket_id="",
        round_no=round_no,
        location=_pick(raw, "location"),
        option_a=_pick(raw, "option_a"),
        option_b=_pick(raw, "option_b"),
        architect_preference=_pick(raw, "architect_preference"),
        impact=_parse_impact(_pick(raw, "impact")),
    )


def generate_tickets(round_no: int, design_text: str, llm: "LLM") -> list[QATicket]:
    """设计文本 → 質問票清单（ARCHITECT 位，转译即提问）。

    票号由本模块分配（`R{round}-{seq:03d}`，D1-1 / R-3），模型/mock 都不许自己编号；
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

    硬规则（D1-1 / R-13）：存在未关闭的 **HIGH** 票时**必须拒绝**（CLI 退出码 3）；
    MEDIUM / LOW 未关闭不阻塞出包。
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
    lines = (
        [str(p).strip() for p in data.get("feedback") if str(p).strip()]
        if isinstance(data.get("feedback"), list)
        else []
    )
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
    """评审位（REVIEWER）提示词：强制全新上下文的对抗性评审（不参与门禁判定，R-5）。"""
    return _render(
        "review.j2",
        package=package,
        package_md=render_task_package(package),
        exec_result=exec_result,
        gate=gate,
    )


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
    """落库質問票：**已存在的票不覆盖**（人工确认结论不能被重跑冲掉，D1-8 / R-6 幂等）。"""
    saved: list[QATicket] = []
    for ticket in tickets:
        existing: Optional[QATicket] = None
        if hasattr(store, "get_ticket"):
            existing = store.get_ticket(ticket.ticket_id)
        saved.append(existing if existing is not None else store.add_ticket(ticket))
    return saved


# ---------------------------------------------------------------- 阶段（图节点共用）


def _stage_tickets(store: "Store", llm: "LLM", round_no: int, design_text: str) -> list[QATicket]:
    _log("[2/6] 質問票转译：ARCHITECT 转译设计（marker KFPA_TASK=qa_tickets）")
    fresh = generate_tickets(round_no, design_text, llm)
    _save_round_tickets(store, fresh)
    all_tickets = store.list_tickets(round_no=round_no)
    _log(f"[2/6] 質問票转译完成：本轮共 {len(all_tickets)} 张（新开 {len(fresh)} 张）")
    return all_tickets


def _scoped_avoid_patterns(store: "Store", round_no: int) -> list[str]:
    """R-28：回灌区只取「本轮已发生的失败」+「上一轮（N-1）的失败」。

    - 本轮：同轮重试必须回灌上一次失败原因（design.md §5.3「失败原因必须回灌新任务包」）；
    - 上一轮：跨轮回灌（spec.md §5.2 原文「回灌区：**上一轮**失败原因与规避要求」）；
    - **禁止全库历史**：那会让 `package_hash` 随库的历史增长而漂移——共享库（PG）与全新库（SQLite）
      对同一轮给出不同 hash，A12 双库对拍必然假红（镜鉴实测过这条）。
    """
    patterns: list[str] = []
    for scope in (round_no, round_no - 1):
        if scope < 1:
            continue
        try:
            patterns.extend(str(p) for p in store.list_avoid_patterns(scope))
        except Exception:  # pragma: no cover - 老库/桩缺该方法时退回空清单
            continue
    seen: list[str] = []
    for item in patterns:
        text = item.strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _stage_package(
    store: "Store",
    llm: "LLM",
    round_no: int,
    design_text: str,
    tickets: list[QATicket],
    avoid_patterns: Optional[list[str]] = None,
) -> TaskPackage:
    avoid = (
        [str(p) for p in avoid_patterns]
        if avoid_patterns is not None
        else _scoped_avoid_patterns(store, round_no)
    )
    _log(f"[4/6] 任务包生成：六段式，回灌区注入 {len(avoid)} 条本轮/上一轮失败模式")
    package = build_task_package(round_no, design_text, tickets, llm, avoid)
    store.save_package(package)
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
    try:
        # R-23.2 来源①：把执行器真实产出落盘，`kfpa fastlane check` 优先读它（不依赖 git 状态）
        (workdir / "files_changed.json").write_text(
            json.dumps(
                {
                    "round_no": package.round_no,
                    "attempt": attempt,
                    "files_changed": list(result.files_changed or []),
                    "executor": getattr(result, "executor", ""),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - 落盘失败不影响门禁
        _log(f"[pipeline] 改动清单落盘失败（{exc}）→ fastlane 将退回 --files / no_diff_source")
    return result


def _stage_gate(
    store: "Store",
    package: TaskPackage,
    exec_result: ExecResult,
    attempt: int,
    coverage: Any = None,
    reviews: Any = None,
) -> GateReport:
    """门禁判定；`coverage` 非 None 才启用 G7（R-22.3），`reviews` 走 G8 折算（D4-3.3）。"""
    report = gates.evaluate(
        package.round_no, package, exec_result, store, attempt=attempt,
        coverage=coverage, reviews=reviews,
    )
    store.save_gate_report(report)
    coverage_note = ""
    if coverage is not None:
        coverage_note = f"｜G7 覆盖率={getattr(coverage, 'line_rate', 0.0):.1%}"
    _log(
        f"[6/6] 门禁判定（第 {attempt} 次尝试）：{report.state.value}"
        f"｜failing={','.join(report.failing_criteria) or '-'}"
        f"｜reason_key={report.reason_key or '-'}{coverage_note}"
    )
    return report


def _stage_snapshot(
    store: "Store",
    package: TaskPackage,
    report: GateReport,
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


# ---------------------------------------------------------------- LangGraph 编排（R-14）


def _reuse_knowledge(store: "Store", package: TaskPackage, round_no: int) -> list[str]:
    """PASS 轮登记沉淀候选复用并转正（D3-3.5）；P0 七表 store 上降级跳过。"""
    if not hasattr(store, "list_candidates"):
        return []
    try:
        return knowledge.reuse_from_package(store, package, round_no, passed=True)
    except Exception as exc:  # pragma: no cover - 沉淀失败不该影响快照
        _log(f"[knowledge] 复用标记失败（{exc}）→ 只警告，不影响本轮快照")
        return []


def _save_snapshot_for_pass(
    store: "Store",
    package: TaskPackage,
    report: GateReport,
    workdir_path: Path,
    round_no: int,
    dry_run: bool,
) -> Snapshot:
    """PASS 轮：快照 + 沉淀闸门复用标记（D3-3.5）。"""
    snapshot = _stage_snapshot(store, package, report, workdir_path, round_no, dry_run)
    promoted = _reuse_knowledge(store, package, round_no)
    if promoted:
        _log(f"[knowledge] 本轮复用触发转正 {len(promoted)} 条：{'、'.join(promoted)}")
    return snapshot


def build_graph(
    *,
    store: "Store",
    llm: "LLM",
    executor: "Executor",
    workdir: str | Path = ".",
    max_attempts: int = 3,
    dry_run: bool = True,
    injection: str = "none",
    checkpointer: Any = None,
    coverage: Any = None,
    coverage_json: str | Path | None = None,
    review: bool = True,
    review_provider: str = "mock_hetero",
    review_model_ref: str = "",
) -> Any:
    """编译 LangGraph `StateGraph`（签名与 state 契约见 D2 R-14）。

    用法::

        app = build_graph(store=store, llm=llm, executor=executor, workdir=tmp)
        final = app.invoke({"round_no": 1, "design_text": text, "design_ref": "examples/design-r1.md"})
        final["state"], final["gate"].state          # 终止态

    - 节点按 `GRAPH_NODES` 冻结：`gen_tickets → guard_tickets → build_package → execute → gate → finalize`；
      未关闭 HIGH 票 → 直接 `finalize`（`state=TICKETS_OPEN`，CLI 退出码 3）；
      `gate` 判 NEEDS_FIX 且 `attempt < max_attempts` → 带 `avoid_patterns` 回 `execute`（重建任务包，D1-11）。
    - 节点只调纯函数 + Store + 执行器，节点内不写判据（D1-12 约束 2）。
    - `checkpointer=None` 时**不挂 checkpointer**，保证 R-14 的「`invoke` 只给三个入参」可直接跑；
      传入自定义 checkpointer 时由调用方按 LangGraph 约定给 `config`（如 `thread_id`）。
    - **G7（R-22.3）**：只有给了 `coverage`（`CoverageReport`）或 `coverage_json`（pytest-cov 报告路径）
      时才启用；都不给 → 门禁跳过 G7，默认 happy path 不因此判红。
    """
    coverage_report = coverage
    if coverage_report is None and coverage_json:
        coverage_report = gates.load_coverage_report(coverage_json)
    # P2 D4-3.3：REVIEWER 接入主循环的开关与留痕信息（provider 只记录，实际路由由 llm 按角色完成）
    review_enabled = bool(review)
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover - 依赖由 Chief 统一安装
        raise DependencyMissingError(
            "[pipeline] 缺少 langgraph（编排层） → 让 Chief 按 D1-12 统一安装（退出码 4）"
        ) from exc

    workdir_path = Path(workdir)
    if injection in EXECUTOR_INJECTIONS and hasattr(executor, "injection"):
        executor.injection = injection  # type: ignore[attr-defined]

    # ---------------------------------------------------------- 节点

    def n_gen_tickets(state: RoundGraphState) -> dict:
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
        tickets = _stage_tickets(store, llm, round_no, str(state["design_text"]))
        # 入口节点显式重置本轮运行态：挂了 checkpointer 后，同一 thread_id 重跑会把
        # 上一轮的 gate/package/attempt 合并进 state，必须清零，否则会用陈旧门禁结果走 finalize。
        return {
            "tickets": tickets,
            "state": RoundState.TICKETS_OPEN,
            "open_high": [],
            "task_package": None,
            "exec_result": None,
            "gate": None,
            "snapshot": None,
            "attempt": 0,
            "avoid_patterns": [],
            "message": "",
        }

    def n_guard_tickets(state: RoundGraphState) -> dict:
        round_no = int(state["round_no"])
        # R-13：只有「未关闭的 HIGH 票」阻塞；MEDIUM/LOW 走攒批确认
        open_high = [t.ticket_id for t in store.list_open_high_tickets(round_no)]
        if open_high:
            _log(f"[3/6] 硬规则闸：拦停，未关闭高影响票 {len(open_high)} 张（{'、'.join(open_high)}）")
            return {"open_high": open_high, "state": RoundState.TICKETS_OPEN}
        _log("[3/6] 硬规则闸：通过（无未关闭高影响票；MEDIUM/LOW 不阻塞）")
        store.set_round_state(round_no, RoundState.RELEASED)
        return {"open_high": [], "state": RoundState.RELEASED}

    def route_guard(state: RoundGraphState) -> str:
        return "finalize" if state.get("open_high") else "build_package"

    def n_build_package(state: RoundGraphState) -> dict:
        avoid = _scoped_avoid_patterns(store, int(state["round_no"]))
        package = _stage_package(
            store,
            llm,
            int(state["round_no"]),
            str(state["design_text"]),
            state.get("tickets") or [],
            avoid,
        )
        return {"task_package": package, "avoid_patterns": avoid}

    def n_execute(state: RoundGraphState) -> dict:
        attempt = int(state.get("attempt", 0)) + 1
        avoid = [str(p) for p in (state.get("avoid_patterns") or [])]
        package = state.get("task_package")
        # 重试边「带 avoid_patterns 回 execute」：用回灌区重建任务包（D1-11）
        if attempt > 1 or (package is not None and avoid and list(package.feedback) != avoid):
            package = _stage_package(
                store,
                llm,
                int(state["round_no"]),
                str(state["design_text"]),
                state.get("tickets") or [],
                avoid,
            )
        if package is None:  # pragma: no cover - build_package 必先于 execute
            raise PipelineError("[pipeline] execute 缺少任务包 → 检查 build_package 入边")
        exec_result = _stage_execute(store, executor, package, workdir_path, attempt)
        return {"attempt": attempt, "task_package": package, "exec_result": exec_result}

    def n_gate(state: RoundGraphState) -> dict:
        package = state.get("task_package")
        exec_result = state.get("exec_result")
        if package is None or exec_result is None:  # pragma: no cover
            raise PipelineError("[pipeline] gate 缺少任务包或执行结果 → 检查 execute 出边")
        round_no = int(state["round_no"])
        attempt = int(state.get("attempt", 1))

        # P2 D4-3.3：REVIEWER 接入主循环（fresh context），发现按 G8 折算进本次门禁
        reviews: Any = None
        if review_enabled:
            try:
                review_report = review_mod.run_review(
                    package,
                    llm,
                    provider=review_provider,
                    round_no=round_no,
                    attempt=attempt,
                    model_ref=review_model_ref,
                )
                review_mod.persist_report(store, review_report)
                if review_report.parse_ok:
                    reviews = review_mod.review_findings_as_dicts(review_report)
                else:
                    # R-5 / A22：解析失败只记 reasons，不改三态
                    reviews = [{
                        "severity": "info",
                        "location": f"review:{review_report.context_id}",
                        "issue": "评审输出解析失败 → 只记录，不改三态（R-5）",
                    }]
                _log(
                    f"[6/6] 评审位（{review_report.provider}，context={review_report.context_id}）："
                    f"parse_ok={review_report.parse_ok} verdict={review_report.verdict} "
                    f"findings={len(review_report.findings)} blockers={len(review_report.blockers())}"
                )
            except Exception as exc:  # 评审位异常不得让主循环崩（R-5 语义）
                _log(f"[6/6] 评审位调用异常（{exc}）→ 只记录，不改三态")
                reviews = [{
                    "severity": "info",
                    "location": "review:error",
                    "issue": f"评审位调用异常（{type(exc).__name__}）→ 只记录，不改三态",
                }]

        report = _stage_gate(store, package, exec_result, attempt, coverage_report, reviews)
        return {
            "gate": report,
            "avoid_patterns": _scoped_avoid_patterns(store, round_no),
        }

    def route_gate(state: RoundGraphState) -> str:
        report = state.get("gate")
        if report is None:  # pragma: no cover
            return "finalize"
        if report.state is GateState.PASS or report.state is GateState.BLOCKED:
            return "finalize"
        if int(state.get("attempt", 1)) < max(1, max_attempts):
            return "execute"
        return "finalize"

    def n_finalize(state: RoundGraphState) -> dict:
        round_no = int(state["round_no"])
        tickets = state.get("tickets") or []
        package = state.get("task_package")
        exec_result = state.get("exec_result")
        report = state.get("gate")

        # ① 未关闭 HIGH 票拦停（R-13 / R-1 退出码 3）
        if state.get("open_high") and report is None:
            ids = list(state["open_high"])
            store.set_round_state(round_no, RoundState.TICKETS_OPEN)
            message = (
                f"{BLOCKED_MARK} [pipeline] 第 {round_no} 轮拦停：{len(ids)} 张未关闭高影响票"
                f"（{'、'.join(ids)}） → 先 `kfpa tickets close` 攒批确认后重跑本轮（退出码 3）"
            )
            _log(message)
            return {
                "state": RoundState.TICKETS_OPEN,
                "tickets": tickets,
                "task_package": None,
                "exec_result": None,
                "gate": None,
                "snapshot": None,
                "message": message,
            }

        # ② 重试上限用尽且未升级 → attempts_exhausted 终态（D1-5）
        if report is not None and report.state is GateState.NEEDS_FIX:
            attempt = int(state.get("attempt", 1))
            if package is not None:  # 终止前补齐回灌区（失败必回灌，D1-11）
                avoid = _scoped_avoid_patterns(store, round_no)
                package.feedback = avoid or [
                    f"第 {attempt} 次尝试失败：{'；'.join(report.reasons)}"
                ]
                store.save_package(package)
            report = gates.exhausted_report(round_no, attempt, store, last=report)
            store.save_gate_report(report)

        if report is None:  # pragma: no cover
            raise PipelineError("[pipeline] 图终止态缺少门禁结果 → 检查 finalize 入边")

        if report.state is GateState.PASS:
            if package is None:  # pragma: no cover
                raise PipelineError("[pipeline] PASS 终止态缺少任务包 → 检查 build_package 入边")
            snapshot = _save_snapshot_for_pass(
                store, package, report, workdir_path, round_no, dry_run
            )
            workdir_path.mkdir(parents=True, exist_ok=True)
            (workdir_path / "review-prompt.md").write_text(
                build_review_prompt(package, exec_result, report), encoding="utf-8"
            )
            message = (
                f"[pipeline] 第 {round_no} 轮门禁 PASS（第 {state.get('attempt')} 次尝试）："
                f"package_hash={snapshot.package_hash}，已快照 tag={snapshot.tag}"
            )
            _log(message)
            return {
                "state": RoundState.SNAPSHOTTED,
                "tickets": tickets,
                "task_package": package,
                "exec_result": exec_result,
                "gate": report,
                "snapshot": snapshot,
                "message": message,
            }

        store.set_round_state(round_no, RoundState.BLOCKED)
        message = (
            f"{BLOCKED_MARK} [gate] 第 {round_no} 轮 BLOCKED（第 {state.get('attempt')} 次尝试）："
            f"同类失败第 {report.same_reason_count + 1} 次（阈值 {gates.ESCALATE_THRESHOLD}）"
            " → 先做变更影响分析再重规划（退出码 11）"
        )
        _log(message)
        return {
            "state": RoundState.BLOCKED,
            "tickets": tickets,
            "task_package": package,
            "exec_result": exec_result,
            "gate": report,
            "snapshot": None,
            "message": message,
        }

    # ---------------------------------------------------------- 图

    graph = StateGraph(RoundGraphState)
    graph.add_node("gen_tickets", n_gen_tickets)
    graph.add_node("guard_tickets", n_guard_tickets)
    graph.add_node("build_package", n_build_package)
    graph.add_node("execute", n_execute)
    graph.add_node("gate", n_gate)
    graph.add_node("finalize", n_finalize)

    graph.set_entry_point("gen_tickets")
    graph.add_edge("gen_tickets", "guard_tickets")
    graph.add_conditional_edges(
        "guard_tickets", route_guard, {"finalize": "finalize", "build_package": "build_package"}
    )
    graph.add_edge("build_package", "execute")
    graph.add_edge("execute", "gate")
    graph.add_conditional_edges("gate", route_gate, {"finalize": "finalize", "execute": "execute"})
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer) if checkpointer is not None else graph.compile()


def _result_from_state(state: dict) -> RoundResult:
    """把图终止态组装成 `RoundResult`（R-14：终止态字段与 RoundResult 同型）。"""
    return RoundResult(
        round_no=int(state["round_no"]),
        state=state["state"],
        tickets=list(state.get("tickets") or []),
        task_package=state.get("task_package"),
        exec_result=state.get("exec_result"),
        gate=state.get("gate"),
        snapshot=state.get("snapshot"),
        message=str(state.get("message") or ""),
    )


# ---------------------------------------------------------------- 对外轮次入口


def default_checkpoint_path(workdir: str | Path) -> Path:
    """R-22.2 冻结口径：checkpoint 库固定为 `{workdir}/checkpoints.sqlite`。"""
    return Path(workdir) / CHECKPOINT_DB_NAME


def make_checkpointer(path: str | Path | None = None) -> Any:
    """构造 P1 默认 checkpoint（`SqliteSaver`，D3-3.5 / R-22.2）。

    - 缺 `langgraph-checkpoint-sqlite` → `DependencyMissingError`（CLI 映射退出码 4）；
    - `path=None` → 用当前目录下的 `CHECKPOINT_DB_NAME`（正常路径由 `run_round` 传 workdir）；
      父目录自动创建。
    """
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:  # pragma: no cover - 依赖由 Chief 统一安装
        raise DependencyMissingError(
            "[pipeline] 缺少 langgraph-checkpoint-sqlite（P1 checkpoint）"
            " → 让 Chief 按 D3 §5 统一安装；或用 `checkpointer=None` 关闭（退出码 4）"
        ) from exc
    target = Path(path or CHECKPOINT_DB_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    # SqliteSaver.from_conn_string() 返回上下文管理器，不能直接当 saver 传给 compile()；
    # 这里自持连接（check_same_thread=False）并由 saver 持有引用，进程内有效、落盘持久。
    connection = sqlite3.connect(str(target), check_same_thread=False)
    return SqliteSaver(connection)


def _resolve_checkpointer(checkpointer: Any, round_no: int, workdir: Path) -> Any:
    """解析 checkpointer（R-22.2 + CLI `--checkpoint-db`）：

    - `None` → 不挂（P0 的三键 invoke 与 A8 仍可跑）；
    - `"auto"`（默认）→ `{workdir}/checkpoints.sqlite` 上的 `SqliteSaver`；
    - 其它字符串 → 当成 `--checkpoint-db` 给的路径；
    - 其它对象 → 原样作为 saver 传给 `compile()`。
    """
    if checkpointer is None:
        return None
    if checkpointer == "auto":
        _ = round_no  # thread_id 由调用方按 `kfpa-r{n}` 传给 invoke
        return make_checkpointer(default_checkpoint_path(workdir))
    if isinstance(checkpointer, (str, Path)):
        return make_checkpointer(checkpointer)
    return checkpointer


def _default_roles() -> dict[str, dict]:
    """尽力读 `config/models.yaml` 的角色表（拿不到就空表：model_ref 记空串，不编造）。"""
    try:
        return ledger_mod.roles_from_config()
    except Exception:  # pragma: no cover - 缺配置不该拖垮流水线
        return {}


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
    checkpointer: Any = "auto",
    ledger: Any = None,
    ledger_source: str = "mock",
    roles: Optional[dict[str, dict]] = None,
    coverage: Any = None,
    coverage_json: str | Path | None = None,
    review: bool = True,
    review_provider: str = "mock_hetero",
    review_model_ref: str = "",
) -> RoundResult:
    """跑一轮完整生命周期 —— `build_graph(...).invoke(...)` 的**薄包装**（R-14 + P1 D3-3.4/3.5 + P2 D4-3.3）。

    - 未关闭 HIGH 票 → `state=TICKETS_OPEN` + `BLOCKED_MARK`（CLI 退出码 3）；
    - `injection` 只把 `apply_fail` / `test_fail` 转给执行器（R-10）；
    - 不同原因连续失败到 `max_attempts` → BLOCKED + `attempts_exhausted`（D1-5）；
    - **P1**：`checkpointer="auto"` → 默认 `SqliteSaver`（`{workdir}/checkpoints.sqlite`，R-22.2；
      `thread_id = f"kfpa-r{round_no}"`，同轮重跑复用；显式传 `None` 关闭）；
      每次 `LLM.complete()` 经账本 wrapper 落一条 `LedgerEntry`（D3-3.4）；
      PASS 轮把「任务包真的用到的隔离区候选」标记复用并按规则转正（D3-3.5）；
      `coverage` / `coverage_json` 只在显式提供时启用 G7（R-22.3）。
    - **P2**：`review=True` 时每轮门禁前调 REVIEWER（fresh context，D4-3.3），发现按 G8 折算并留痕。
    """
    if ledger is None:
        ledger = ledger_mod.make_ledger(store)
    resolved_roles = roles if roles is not None else _default_roles()
    resolved_checkpointer = _resolve_checkpointer(checkpointer, round_no, Path(workdir))
    graph = build_graph(
        store=store,
        llm=ledger_mod.wrap_llm(
            llm,
            ledger,
            round_no=round_no,
            roles=resolved_roles,
            source=ledger_source,
        ),
        executor=executor,
        workdir=workdir,
        max_attempts=max_attempts,
        dry_run=dry_run,
        injection=injection,
        checkpointer=resolved_checkpointer,
        coverage=coverage,
        coverage_json=coverage_json,
        review=review,
        review_provider=review_provider,
        review_model_ref=review_model_ref,
    )
    payload = {"round_no": round_no, "design_text": design_text, "design_ref": design_ref}
    final = (
        graph.invoke(payload, {"configurable": {"thread_id": f"kfpa-r{round_no}"}})
        if resolved_checkpointer is not None
        else graph.invoke(payload)
    )
    if not isinstance(final, dict) or "state" not in final:
        raise PipelineError(
            "[pipeline] 图未返回含 state 的终止态 → 检查 finalize 节点（R-14）"
        )
    return _result_from_state(final)


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
    checkpointer: Any = "auto",
    coverage: Any = None,
    coverage_json: str | Path | None = None,
    review: bool = True,
    review_provider: str = "mock_hetero",
    review_model_ref: str = "",
) -> RoundResult:
    """兼容别名：语义与 `run_round` 完全一致（两者都走 LangGraph）。"""
    return run_round(
        round_no,
        design_text,
        design_ref,
        store=store,
        llm=llm,
        executor=executor,
        workdir=workdir,
        max_attempts=max_attempts,
        dry_run=dry_run,
        injection=injection,
        checkpointer=checkpointer,
        coverage=coverage,
        coverage_json=coverage_json,
        review=review,
        review_provider=review_provider,
        review_model_ref=review_model_ref,
    )


__all__ = [
    "PROMPTS_DIR",
    "CHECKPOINT_DB_NAME",
    "BLOCKED_MARK",
    "EXECUTOR_INJECTIONS",
    "MAX_TICKETS_PER_ROUND",
    "IMPACT_FLOOR_HIGH",
    "IMPACT_FLOOR_MEDIUM",
    "GRAPH_NODES",
    "RoundGraphState",
    "PipelineError",
    "DependencyMissingError",
    "OpenHighTicketsError",
    "apply_impact_floor",
    "cap_tickets",
    "generate_tickets",
    "build_task_package",
    "render_task_package",
    "build_review_prompt",
    "make_checkpointer",
    "default_checkpoint_path",
    "run_round",
    "run_round_via_graph",
    "build_graph",
]

"""KFPA v2 冻结数据契约（CONTRACT_VERSION 1.4）。

本文件由 Chief 冻结：**任何人不得直接修改**。需要变更时在群里提，
由 Chief 升版后统一改，改完必须同步 docs/dev/p0-plan.md §3.1（P0）与 docs/dev/p1-plan.md §3.1（P1）。

变更记录：
- 1.0 首版冻结（QATicket / TaskPackage / GateReport / ExecResult / ...）
- 1.1 依 docs/dev/p0-design-addendum.md D1-3 / D1-4 补齐门禁所需字段：
      ExecResult 增加 error_category / failed_criteria / results；
      GateReport 增加 reason_key。
- 1.2 依 docs/dev/p1-plan.md D3-3.1 增加 P1 三件：LedgerEntry（token 账本）、
      KnowledgeCandidate + CandidateStatus（沉淀闸门）、CoverageReport（G7 覆盖率门槛）。
- 1.3 依 docs/dev/p2-plan.md D4-3.1 增加 P2 三件：Severity + ReviewFinding + ReviewReport
      （异构评审位）、ReviewMetrics（同源盲区度量与通过判据）。

设计约束：
- 只用标准库（dataclasses / enum / json / hashlib），不吃第三方依赖，
  保证离线 dry-run 与 pytest 在零依赖环境也能跑。
- 所有枚举都继承 str，可直接 json.dumps。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

CONTRACT_VERSION = "1.4"

# ---------------------------------------------------------------- 枚举


class GateState(str, Enum):
    """质量门禁三态（design.md §5.3）。"""

    PASS = "pass"          # 通过 → 按轮快照入库
    NEEDS_FIX = "needs_fix"  # 需修复 → Flash 自主重试，失败原因必须回灌
    BLOCKED = "blocked"    # 阻塞 → 升级主BOT，做变更影响分析后重规划


class TicketImpact(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TicketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    DROPPED = "dropped"


class RoundState(str, Enum):
    DESIGN_IN = "design_in"
    TICKETS_OPEN = "tickets_open"   # 已产出質問票，等人工攒批确认
    RELEASED = "released"           # 高影响票已清零，放行
    EXECUTING = "executing"
    GATED = "gated"
    SNAPSHOTTED = "snapshotted"
    BLOCKED = "blocked"


# ---------------------------------------------------------------- 質問票

#: 票面五要素（design.md §5.1），缺一不可
TICKET_REQUIRED_FIELDS = (
    "ticket_id",
    "location",
    "option_a",
    "option_b",
    "architect_preference",
)


@dataclass
class QATicket:
    """質問票：主BOT 转译设计时发现的歧义。"""

    ticket_id: str
    round_no: int
    location: str                 # 设计书章节 / 行号
    option_a: str                 # 可能解释 A
    option_b: str                 # 可能解释 B
    architect_preference: str     # 主BOT 倾向（A/B/说明）
    impact: TicketImpact = TicketImpact.MEDIUM
    status: TicketStatus = TicketStatus.OPEN
    answer: Optional[str] = None  # 人工确认结果
    created_at: float = field(default_factory=time.time)

    def missing_fields(self) -> list[str]:
        """返回缺失的票面必填项（空列表 = 票面完整）。"""
        missing: list[str] = []
        for name in TICKET_REQUIRED_FIELDS:
            value = getattr(self, name)
            if value is None or not str(value).strip():
                missing.append(name)
        return missing

    def is_open_high(self) -> bool:
        """未关闭的高影响票 —— 它存在就不得生成任务包（硬规则）。"""
        return self.impact is TicketImpact.HIGH and self.status is TicketStatus.OPEN


# ---------------------------------------------------------------- 任务包

#: 六段式任务包（design.md §5.2）。feedback 是回灌区，首轮可为空。
TASK_SECTIONS = (
    "goal",
    "acceptance",
    "boundaries",
    "interface_contract",
    "prohibitions",
    "feedback",
)


@dataclass
class AcceptanceCriterion:
    """可测试的验收断言（任务包第 2 段的一条）。"""

    cid: str
    text: str

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not str(self.cid or "").strip():
            missing.append("cid")
        if not str(self.text or "").strip():
            missing.append("text")
        return missing


@dataclass
class TaskPackage:
    """编码任务包 —— 全系统最贵的资产（design.md §5.2）。"""

    round_no: int
    feature: str
    goal: str                                        # 段1 目标描述
    acceptance: list[AcceptanceCriterion] = field(default_factory=list)   # 段2 验收标准
    boundaries: list[str] = field(default_factory=list)                   # 段3 边界条件
    interface_contract: list[str] = field(default_factory=list)           # 段4 接口契约
    prohibitions: list[str] = field(default_factory=list)                 # 段5 禁止事项
    feedback: list[str] = field(default_factory=list)                     # 段6 回灌区（首轮可空）
    closed_tickets: list[str] = field(default_factory=list)               # 已关闭并可据此消歧的票号
    knowledge_hits: list[str] = field(default_factory=list)               # P3 契约 1.4：检索命中摘要（稳定串）

    def missing_sections(self) -> list[str]:
        """返回结构性缺段（空列表 = 六段结构齐全）。

        feedback（回灌区）首轮允许为空，但渲染时必须有「无（首轮）」占位段，
        因此不计入缺段判定；其余五段必须有实质内容。
        """
        missing: list[str] = []
        if not str(self.goal or "").strip():
            missing.append("goal")
        if not self.acceptance:
            missing.append("acceptance")
        if not self.boundaries:
            missing.append("boundaries")
        if not self.interface_contract:
            missing.append("interface_contract")
        if not self.prohibitions:
            missing.append("prohibitions")
        return missing

    def is_complete(self) -> bool:
        return not self.missing_sections() and not any(
            c.missing_fields() for c in self.acceptance
        )


def package_hash(package: TaskPackage) -> str:
    """任务包内容指纹（快照绑定用），稳定、可复算。"""
    payload = json.dumps(to_jsonable(package), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 执行与门禁


@dataclass
class ExecResult:
    """执行器（Flash / Aider / mock）一次应用的返回。

    error_category 取值见 docs/dev/p0-design-addendum.md D1-4：
    apply_failed / compile / test_fail / missing_section / unmapped_criterion / other
    results：验收断言 cid -> 是否通过（门禁 G4 据此判「断言未映射」）。
    """

    ok: bool
    files_changed: list[str] = field(default_factory=list)
    diff: str = ""
    error: str = ""
    error_category: str = ""
    failed_criteria: list[str] = field(default_factory=list)
    results: dict[str, bool] = field(default_factory=dict)
    executor: str = "mock"


@dataclass
class FailureRecord:
    """失败记录 → 回避模式清单（design.md §5.5，负知识同样值钱）。"""

    round_no: int
    attempt: int
    reason: str
    avoid_pattern: str
    reason_key: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class GateReport:
    """质量门禁判定结果（三态）。"""

    round_no: int
    state: GateState
    reasons: list[str] = field(default_factory=list)
    failing_criteria: list[str] = field(default_factory=list)
    attempt: int = 1
    escalated: bool = False       # 同类失败 >=2 次自动升级主BOT
    same_reason_count: int = 0    # 历史同类失败次数（不含本次）
    reason_key: str = ""          # 本次失败归一化键（PASS 时为空串）
    created_at: float = field(default_factory=time.time)


@dataclass
class Snapshot:
    """按轮快照：代码 + 任务包 + 测试结果绑定（design.md §3.1-6）。"""

    round_no: int
    package_hash: str
    gate_state: GateState
    git_sha: str = ""
    tag: str = ""
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------- 轮次


@dataclass
class Round:
    round_no: int
    design_ref: str
    state: RoundState = RoundState.DESIGN_IN
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class RoundResult:
    """run_round() 的返回：一轮的完整痕迹。"""

    round_no: int
    state: RoundState
    tickets: list[QATicket] = field(default_factory=list)
    task_package: Optional[TaskPackage] = None
    exec_result: Optional[ExecResult] = None
    gate: Optional[GateReport] = None
    snapshot: Optional[Snapshot] = None
    message: str = ""

    def open_high_tickets(self) -> list[str]:
        return [t.ticket_id for t in self.tickets if t.is_open_high()]


# ---------------------------------------------------------------- P1 增量（契约 1.2）


@dataclass
class LedgerEntry:
    """token 账本一条（P1 D3-3.4）：每次 LLM 调用落一条，失败经济学要算账。"""

    round_no: int
    role: str                     # ARCHITECT / REVIEWER / FLASH / EMBEDDING
    model_ref: str                # 从 config/models.yaml 的 model 字段取，代码里不许写死
    tokens_in: int
    tokens_out: int
    cost: float = 0.0             # 未配置单价时为 0.0，不许编造
    source: str = "mock"          # mock | litellm | langfuse
    created_at: float = field(default_factory=time.time)

    def total_tokens(self) -> int:
        return int(self.tokens_in) + int(self.tokens_out)


class CandidateStatus(str, Enum):
    QUARANTINE = "quarantine"     # 待验证隔离区
    PROMOTED = "promoted"         # 已转正
    DROPPED = "dropped"           # 不合格，直接丢弃


@dataclass
class KnowledgeCandidate:
    """知識沉淀候选（P1 D3-3.5 / design.md §5.5 防中毒）。"""

    cid: str
    round_no: int
    kind: str                     # success | avoid（负知识同样值钱）
    content: str
    confidence: float             # 0.0 ~ 1.0
    status: CandidateStatus = CandidateStatus.QUARANTINE
    reused_by: list[int] = field(default_factory=list)   # 成功复用过它的轮次号
    created_at: float = field(default_factory=time.time)

    def can_promote(self) -> bool:
        """转正条件：至少被 1 个成功轮次复用（D3-3.5）。"""
        return self.status is CandidateStatus.QUARANTINE and len(self.reused_by) >= 1


@dataclass
class CoverageReport:
    """覆盖率门槛报告（P1 D3-3.3 G7）。"""

    line_rate: float              # 0.0 ~ 1.0
    files: dict[str, float] = field(default_factory=dict)   # 文件 -> 行覆盖率
    critical_paths_ok: bool = False    # A1–A10 台架是否全绿
    threshold_line: float = 0.80

    def failing_files(self) -> list[str]:
        return sorted(f for f, rate in self.files.items() if rate < self.threshold_line)

    def passed(self) -> bool:
        return self.line_rate >= self.threshold_line and self.critical_paths_ok


# ---------------------------------------------------------------- P2 增量（契约 1.3）


class Severity(str, Enum):
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


@dataclass
class ReviewFinding:
    """评审位的一条发现（P2 D4-3.1）。"""

    severity: Severity
    location: str
    issue: str
    suggestion: str = ""
    criterion: str = ""       # 对应验收断言 cid；空串 = 无法定位（G8 不阻塞）


@dataclass
class ReviewReport:
    """一次评审的结果（P2 D4-3.3）。"""

    round_no: int
    provider: str             # mock_hetero | mock | litellm
    model_ref: str            # 从 config/models.yaml 取，不许写死
    context_id: str           # fresh context 标识（不得等于 ARCHITECT 的）
    parse_ok: bool
    verdict: str              # pass | fail | unknown
    findings: list[ReviewFinding] = field(default_factory=list)
    attempt: int = 1
    created_at: float = field(default_factory=time.time)

    def blockers(self) -> list[ReviewFinding]:
        """G8：只有 major 且带 criterion 的发现才阻塞。"""
        return [
            f for f in self.findings
            if f.severity is Severity.MAJOR and str(f.criterion or "").strip()
        ]


@dataclass
class ReviewMetrics:
    """同源盲区度量结果（P2 D4-3.4）。"""

    corpus: str
    total: int
    missed: int
    false_alarms: int
    agreement: float
    baseline_missed: int = 0
    baseline_false_alarms: int = 0
    baseline_agreement: float = 0.0

    def miss_rate(self) -> float:
        return 0.0 if self.total <= 0 else self.missed / self.total

    def false_alarm_rate(self) -> float:
        return 0.0 if self.total <= 0 else self.false_alarms / self.total

    def passed(self) -> bool:
        """P2 通过判据（D4-3.4）：漏检不增、误报不增、一致率不降。"""
        return (
            self.missed <= self.baseline_missed
            and self.false_alarms <= self.baseline_false_alarms
            and self.agreement >= self.baseline_agreement
        )


# ---------------------------------------------------------------- P3 增量（契约 1.4）


class FastlaneStatus(str, Enum):
    """快车道请求状态（P3 D5-3.1，兑现 R-23 的「P3 落库」）。"""

    PENDING = "pending"        # 已登记待承认
    APPROVED = "approved"      # 攒批承认通过（唯一可直跑的态）
    REJECTED = "rejected"      # 人工拒绝
    ESCALATED = "escalated"    # 触碰设计边界 → 升级回完整设计回合


@dataclass
class FastlaneRequest:
    """快车道请求（design.md §5.6 的「小变更 → 微型规格 → 攒批承认 → Flash 直跑」）。"""

    request_id: str
    round_no: int
    summary: str
    diff_files: list[str] = field(default_factory=list)    # 三来源优先级沿用 R-23
    new_interface: bool = False                            # 判据 1
    new_data_model: bool = False                           # 判据 2
    touched_modules: list[str] = field(default_factory=list)   # 判据 3（>1 → 不符）
    status: FastlaneStatus = FastlaneStatus.PENDING
    batch_id: str = ""
    decided_by: str = ""
    decided_at: float = 0.0
    note: str = ""

    def judged_fastlane(self) -> bool:
        """三判据全真**且 diff 来源可得**才算快车道（D5-3.2 / R-23 裁决 2）。

        判定只做纯计算，编排不进这里。`diff_files` 为空 = 没有 diff 来源（R-23 的第三来源
        `no_diff_source`）→ 波及面无从判断，按 R-23「不猜、不默认 True」返回 False。
        """
        return (
            not self.new_interface
            and not self.new_data_model
            and len(set(self.touched_modules)) <= 1
            and bool(self.diff_files)
        )

    def escalations(self) -> list[str]:
        """不满足判据的原因清单 —— 不许只给布尔（D5-3.2 / R-35）。"""
        reasons: list[str] = []
        if not self.diff_files:
            reasons.append("no_diff_source")      # R-23 第三来源：无 diff 来源 = 不可判
        if self.new_interface:
            reasons.append("new_interface")
        if self.new_data_model:
            reasons.append("new_data_model")
        if len(set(self.touched_modules)) > 1:
            reasons.append("touched_modules>1")
        return reasons


@dataclass
class KnowledgeSample:
    """人工抽检留痕（design.md §5.5「置信度门槛 + 人工抽检」）。"""

    cid: str
    sampled_at: float = field(default_factory=time.time)
    sampled_by: str = ""
    rate: float = 0.0      # 抽中时的动态抽样率（留痕，便于回看口径）
    passed: bool = True


# ---------------------------------------------------------------- 工具


def to_jsonable(obj: Any) -> Any:
    """dataclass / Enum / list 递归转成可 json.dumps 的裸结构。"""
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def dumps(obj: Any) -> str:
    """稳定序列化（sort_keys），落库与快照都用它。"""
    return json.dumps(to_jsonable(obj), sort_keys=True, ensure_ascii=False)


__all__ = [
    "CONTRACT_VERSION",
    "GateState",
    "TicketImpact",
    "TicketStatus",
    "RoundState",
    "TICKET_REQUIRED_FIELDS",
    "TASK_SECTIONS",
    "QATicket",
    "AcceptanceCriterion",
    "TaskPackage",
    "package_hash",
    "ExecResult",
    "FailureRecord",
    "GateReport",
    "Snapshot",
    "Round",
    "RoundResult",
    "LedgerEntry",
    "CandidateStatus",
    "KnowledgeCandidate",
    "CoverageReport",
    "Severity",
    "ReviewFinding",
    "ReviewReport",
    "ReviewMetrics",
    "FastlaneStatus",
    "FastlaneRequest",
    "KnowledgeSample",
    "to_jsonable",
    "dumps",
]

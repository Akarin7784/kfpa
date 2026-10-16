"""KFPA v2 冻结数据契约（CONTRACT_VERSION 1.1）。

本文件由 Chief 冻结：**任何人不得直接修改**。需要变更时在群里提，
由 Chief 升版后统一改，改完必须同步 docs/dev/p0-plan.md §3.1。

变更记录：
- 1.0 首版冻结（QATicket / TaskPackage / GateReport / ExecResult / ...）
- 1.1 依 docs/dev/p0-design-addendum.md D1-3 / D1-4 补齐门禁所需字段：
      ExecResult 增加 error_category / failed_criteria / results；
      GateReport 增加 reason_key。

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

CONTRACT_VERSION = "1.1"

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
    "to_jsonable",
    "dumps",
]

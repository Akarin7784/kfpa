"""kfpa.store —— 双驱动存储门面（docs/dev/p1-plan.md §3.2）。

- :func:`make_store` —— 唯一的装配入口：`dsn` 为空走 SQLite（默认 `kfpa.db`），
  `postgresql://…` 走 PG 驱动，环境变量 `KFPA_DB_URL` 兜底；
- :class:`StoreProtocol` —— 两个驱动共同实现的方法集（P0 的 18 个 + P1 的 7 个），仅用于类型检查；
- ``Store`` —— **P0 兼容别名**（等于 :class:`~kfpa.store.sqlite.SqliteStore`）。

> 为什么 `Store` 不是 Protocol 本身：P0 契约与台架里有大量 `Store(path)` / `Store(":memory:")`
> 的直接实例化，Protocol 不可实例化，直接改名会让 P0 台架（13/13）回归。故：
> 实例化用 `Store`（= SQLite 实现），类型标注用 `StoreProtocol`，工厂用 `make_store()`。

DSN 打码：日志/报错里出现的 DSN 一律走 :func:`mask_dsn`，密码不得泄漏。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from ..schemas import (
    FailureRecord,
    FastlaneRequest,
    FastlaneStatus,
    GateReport,
    KnowledgeCandidate,
    KnowledgeSample,
    LedgerEntry,
    QATicket,
    ReviewReport,
    Round,
    RoundState,
    Snapshot,
    TaskPackage,
    TicketImpact,
    TicketStatus,
)
from .sqlite import (
    ALL_TABLE_NAMES,
    BUSINESS_TABLES,
    META_TABLE,
    P0_SCHEMA_VERSION,
    P1_SCHEMA_VERSION,
    P2_SCHEMA_VERSION,
    P3_SCHEMA_VERSION,
    P1_TABLE_NAMES,
    P2_TABLE_NAMES,
    P3_TABLE_NAMES,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    TABLE_NAMES,
    SqliteStore,
)

#: P0 兼容别名：`from kfpa.store import Store` 的老代码继续可用
Store = SqliteStore

#: 默认 SQLite 库文件（D1-10）
DEFAULT_SQLITE_PATH = "kfpa.db"

#: 环境变量名（D3 §3.2）
DB_URL_ENV = "KFPA_DB_URL"


@runtime_checkable
class StoreProtocol(Protocol):
    """存储门面方法集（P0 §3.2 + D3 §3.2）。SQLite 与 PG 驱动都必须满足。"""

    # ---- 生命周期
    def init_schema(self) -> None: ...
    def close(self) -> None: ...
    def list_tables(self) -> list[str]: ...
    def get_meta(self, key: str) -> Optional[str]: ...
    def set_meta(self, key: str, value: str) -> None: ...

    # ---- 轮次
    def upsert_round(self, round_: Round) -> Round: ...
    def get_round(self, round_no: int) -> Optional[Round]: ...
    def set_round_state(self, round_no: int, state: RoundState) -> None: ...

    # ---- 質問票
    def add_ticket(self, ticket: QATicket) -> QATicket: ...
    def get_ticket(self, ticket_id: str) -> Optional[QATicket]: ...
    def list_tickets(
        self,
        round_no: Optional[int] = None,
        status: Optional[TicketStatus] = None,
        impact: Optional[TicketImpact] = None,
    ) -> list[QATicket]: ...
    def close_ticket(self, ticket_id: str, answer: str) -> QATicket: ...
    def list_open_high_tickets(self, round_no: int) -> list[QATicket]: ...

    # ---- 任务包
    def save_package(self, package: TaskPackage) -> str: ...
    def get_package(self, round_no: int) -> Optional[TaskPackage]: ...

    # ---- 门禁 / 失败 / 快照
    def save_gate_report(self, report: GateReport) -> None: ...
    def list_gate_reports(self, round_no: int) -> list[GateReport]: ...
    def record_failure(self, failure: FailureRecord) -> None: ...
    def list_avoid_patterns(self, round_no: Optional[int] = None) -> list[str]: ...
    def count_same_failure(self, round_no: int, reason_key: str) -> int: ...
    def save_snapshot(self, snapshot: Snapshot) -> None: ...
    def get_snapshot(self, round_no: int) -> Optional[Snapshot]: ...
    def list_snapshots(self) -> list[Snapshot]: ...

    # ---- P1：token 账本
    def add_ledger_entry(self, entry: LedgerEntry) -> None: ...
    def list_ledger(self, round_no: Optional[int] = None) -> list[LedgerEntry]: ...

    # ---- P1：知识沉淀
    def add_candidate(self, c: KnowledgeCandidate) -> None: ...
    def get_candidate(self, cid: str) -> Optional[KnowledgeCandidate]: ...
    def list_candidates(self, status: Optional[str] = None) -> list[KnowledgeCandidate]: ...
    def promote_candidate(self, cid: str, reused_by_round: int) -> KnowledgeCandidate: ...
    def drop_candidate(self, cid: str, reason: str) -> None: ...

    # ---- P2：评审留痕（D4-3.3 / A19）
    def add_review_report(self, report: ReviewReport) -> None: ...
    def list_review_reports(self, round_no: Optional[int] = None) -> list[ReviewReport]: ...

    # ---- P3：快车道落库与攒批承认（D5-3.2 / A23）
    def add_fastlane_request(self, request: FastlaneRequest) -> FastlaneRequest: ...
    def get_fastlane_request(self, request_id: str) -> Optional[FastlaneRequest]: ...
    def list_fastlane_requests(
        self,
        status: Optional[FastlaneStatus | str] = None,
        round_no: Optional[int] = None,
    ) -> list[FastlaneRequest]: ...
    def ack_fastlane_request(
        self,
        request_id: str,
        by: str = "",
        status: FastlaneStatus | str = FastlaneStatus.APPROVED,
        batch_id: str = "",
        note: str = "",
    ) -> FastlaneRequest: ...

    # ---- P3：人工抽检留痕（D5-3.3 / A25）
    def add_knowledge_sample(self, sample: KnowledgeSample) -> KnowledgeSample: ...
    def list_knowledge_samples(self, cid: Optional[str] = None) -> list[KnowledgeSample]: ...


def mask_dsn(dsn: str) -> str:
    """把 DSN 里的密码打码（日志/报错必须用它）。非 DSN 原样返回。"""
    text = str(dsn or "")
    if "://" not in text:
        return text
    scheme, rest = text.split("://", 1)
    if "@" not in rest:
        return text
    creds, host = rest.rsplit("@", 1)
    if ":" not in creds:
        return f"{scheme}://{creds}@{host}"
    user, _password = creds.split(":", 1)
    return f"{scheme}://{user}:***@{host}"


def is_postgres_dsn(dsn: str) -> bool:
    return str(dsn or "").lower().startswith(("postgresql://", "postgres://"))


def make_store(dsn: Optional[str] = None) -> "StoreProtocol":
    """装配存储驱动（D3 §3.2 + R-22.1 + R-25.1）。

    取值优先级（**CLI 常规约定：显式 flag > 环境变量 > 默认值**）：
    1. 显式 DSN（`postgresql://` / `postgres://` / `sqlite://`）—— 最优先，用户就是要它；
    2. 显式普通路径（`dsn` 非空且不是默认 `kfpa.db` 时）—— 也压过环境变量；
    3. `KFPA_DB_URL` —— 只在没给 `--db`、或给的就是默认 `kfpa.db` 时生效（「全局选驱动」的开关）；
    4. 都没有 → SQLite `kfpa.db`。

    R-25.1 的理由：显式 `--db /tmp/x.db` 被环境变量静默改写，会制造一整类
    「机器上残留 KFPA_DB_URL → 台架临时库全被顶成同一个 PG」的假红。
    """
    explicit = str(dsn or "").strip()
    env_url = os.environ.get(DB_URL_ENV, "").strip()
    is_explicit_dsn = explicit.lower().startswith(("postgresql://", "postgres://", "sqlite://"))
    if is_explicit_dsn:
        url = explicit
    elif explicit and explicit != DEFAULT_SQLITE_PATH:
        url = explicit
    elif env_url:
        url = env_url
    else:
        url = explicit or DEFAULT_SQLITE_PATH
    if not url:
        return Store(DEFAULT_SQLITE_PATH)
    if is_postgres_dsn(url):
        from .postgres import PostgresStore  # 惰性：P0 离线环境不装 psycopg 也能用 SQLite

        return PostgresStore(url)
    if url.lower().startswith("sqlite://"):
        path = url[len("sqlite://"):]
        return Store(path or ":memory:")
    return Store(url)


def migrations_dir() -> Path:
    """`migrations/` 目录（PG DDL 所在），供驱动与验收脚本定位。"""
    return Path(__file__).resolve().parents[3] / "migrations"


__all__ = [
    "Store",
    "StoreProtocol",
    "SqliteStore",
    "make_store",
    "mask_dsn",
    "is_postgres_dsn",
    "migrations_dir",
    "DEFAULT_SQLITE_PATH",
    "DB_URL_ENV",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "P0_SCHEMA_VERSION",
    "P1_SCHEMA_VERSION",
    "P2_SCHEMA_VERSION",
    "P3_SCHEMA_VERSION",
    "TABLE_NAMES",
    "ALL_TABLE_NAMES",
    "BUSINESS_TABLES",
    "P1_TABLE_NAMES",
    "P2_TABLE_NAMES",
    "P3_TABLE_NAMES",
    "META_TABLE",
]


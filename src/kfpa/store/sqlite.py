"""SQLite 状态库（P0 §3.2 + D1-9；P1 增量见 docs/dev/p1-plan.md §3.2）。

表：`meta` + P0 六张业务表（rounds / tickets / task_packages / gate_reports / failures / snapshots）
    + P1 两张（ledger / knowledge）+ P2 一张（reviews）+ P3 两张（fastlane_requests / knowledge_samples）。

设计要点：
- 只用标准库 `sqlite3`；`db_path=":memory:"` 必须可用（镜鉴的用例靠它隔离）。
- JSON 字段一律走 `schemas.dumps`（sort_keys，稳定可复算）；时间戳一律 Unix epoch 秒（UTC）。
- `meta.schema_version = "4"`（P3；P0 为 "1"、P1 为 "2"、P2 为 "3"，见各 P*_SCHEMA_VERSION 常量）。
- **不做业务判定**：门禁三态、硬规则（未关闭高影响票）、沉淀闸门判据都不在这里判，本模块只忠实读写。
- 所有写操作立即 commit；实例可作上下文管理器，也能手动 `close()`。
- 类名 `SqliteStore`；`kfpa.store.Store` 仍是它的别名（P0 契约 `Store(path)` 不破）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .. import schemas
from ..schemas import (
    AcceptanceCriterion,
    CandidateStatus,
    FailureRecord,
    FastlaneRequest,
    FastlaneStatus,
    GateReport,
    GateState,
    KnowledgeCandidate,
    KnowledgeSample,
    LedgerEntry,
    QATicket,
    ReviewFinding,
    ReviewReport,
    Round,
    RoundState,
    Severity,
    Snapshot,
    TaskPackage,
    TicketImpact,
    TicketStatus,
    dumps,
    package_hash,
)


def _enum_value(value: Any) -> str:
    """str/Enum 统一成入库用的裸字符串（CandidateStatus 是 str Enum）。"""
    return str(value.value) if hasattr(value, "value") else str(value)

#: 当前 schema 版本（P3 = "4"，写入 meta.schema_version）
SCHEMA_VERSION = "4"

#: P0 时代的 schema 版本（历史记录/兼容判定用）
P0_SCHEMA_VERSION = "1"

#: P1 的 schema 版本
P1_SCHEMA_VERSION = "2"

#: P2 的 schema 版本（历史常量：`reviews` 表那一代）
P2_SCHEMA_VERSION = "3"

#: P3 的 schema 版本（`SCHEMA_VERSION` 始终等于当前阶段的常量）
P3_SCHEMA_VERSION = "4"
assert SCHEMA_VERSION == P3_SCHEMA_VERSION, "SCHEMA_VERSION 必须等于当前阶段的常量"

#: D1-9 的 meta 表名
META_TABLE = "meta"

#: D1-9 的六张 P0 业务表（数量断言沿用 P0 台架，不要往里塞 P1 表）
BUSINESS_TABLES = (
    "rounds",
    "tickets",
    "task_packages",
    "gate_reports",
    "failures",
    "snapshots",
)

#: P1 新增的两张业务表（docs/dev/p1-plan.md §3.2）
P1_TABLE_NAMES = ("ledger", "knowledge")

#: P2 新增的表（docs/dev/p2-plan.md §3.3：评审留痕）
P2_TABLE_NAMES = ("reviews",)

#: P3 新增的表（docs/dev/p3-plan.md D5-3.2 / §3.3：快车道台账 + 抽检留痕）
P3_TABLE_NAMES = ("fastlane_requests", "knowledge_samples")

#: P0 建库后的表集合（含 meta）——P0 台架 selfcheck_core 依赖此常量
TABLE_NAMES = (META_TABLE, *BUSINESS_TABLES)

#: 当前建库后的完整表集合（含 meta）——A12 对拍与 P1/P2/P3 台架用这个
ALL_TABLE_NAMES = (
    META_TABLE,
    *BUSINESS_TABLES,
    *P1_TABLE_NAMES,
    *P2_TABLE_NAMES,
    *P3_TABLE_NAMES,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_no   INTEGER PRIMARY KEY,
    design_ref TEXT NOT NULL,
    state      TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id            TEXT PRIMARY KEY,
    round_no             INTEGER NOT NULL,
    location             TEXT NOT NULL,
    option_a             TEXT NOT NULL,
    option_b             TEXT NOT NULL,
    architect_preference TEXT NOT NULL,
    impact               TEXT NOT NULL,
    status               TEXT NOT NULL,
    answer               TEXT,
    created_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tickets_round ON tickets(round_no, status, impact);

CREATE TABLE IF NOT EXISTS task_packages (
    round_no     INTEGER PRIMARY KEY,
    package_hash TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no          INTEGER NOT NULL,
    attempt           INTEGER NOT NULL,
    state             TEXT NOT NULL,
    reason_key        TEXT NOT NULL DEFAULT '',
    reasons           TEXT NOT NULL,
    failing_criteria  TEXT NOT NULL,
    escalated         INTEGER NOT NULL DEFAULT 0,
    same_reason_count INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gate_round ON gate_reports(round_no, attempt);

CREATE TABLE IF NOT EXISTS failures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no      INTEGER NOT NULL,
    attempt       INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    avoid_pattern TEXT NOT NULL,
    reason_key    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_failures_key ON failures(round_no, reason_key);

CREATE TABLE IF NOT EXISTS snapshots (
    round_no     INTEGER PRIMARY KEY,
    package_hash TEXT NOT NULL,
    gate_state   TEXT NOT NULL,
    git_sha      TEXT NOT NULL DEFAULT '',
    tag          TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);

-- ------------------------------------------------------------ P1（D3 §3.2）
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no   INTEGER NOT NULL,
    role       TEXT NOT NULL,
    model_ref  TEXT NOT NULL DEFAULT '',
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost       REAL NOT NULL DEFAULT 0.0,
    source     TEXT NOT NULL DEFAULT 'mock',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_round ON ledger(round_no, role);

CREATE TABLE IF NOT EXISTS knowledge (
    cid        TEXT PRIMARY KEY,
    round_no   INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    status     TEXT NOT NULL,
    reused_by  TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_knowledge_status ON knowledge(status, round_no);

-- ------------------------------------------------------------ P2（D4-3.3：评审留痕 / fresh context）
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no   INTEGER NOT NULL,
    attempt    INTEGER NOT NULL DEFAULT 1,
    provider   TEXT NOT NULL,
    model_ref  TEXT NOT NULL DEFAULT '',
    context_id TEXT NOT NULL,
    parse_ok   INTEGER NOT NULL DEFAULT 0,
    verdict    TEXT NOT NULL DEFAULT 'unknown',
    findings   TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reviews_round ON reviews(round_no, attempt);

-- ------------------------------------------------------------ P3（D5-3.2 快车道台账 / D5-3.3 抽检留痕）
-- 与 migrations/0003_fastlane.sql 同构（布尔 0/1、列表 JSON TEXT、时间戳 epoch 秒）。
CREATE TABLE IF NOT EXISTS fastlane_requests (
    request_id      TEXT PRIMARY KEY,
    round_no        INTEGER NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    diff_files      TEXT NOT NULL DEFAULT '[]',
    new_interface   INTEGER NOT NULL DEFAULT 0,
    new_data_model  INTEGER NOT NULL DEFAULT 0,
    touched_modules TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
    batch_id        TEXT NOT NULL DEFAULT '',
    decided_by      TEXT NOT NULL DEFAULT '',
    decided_at      REAL NOT NULL DEFAULT 0.0,
    note            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_fastlane_status ON fastlane_requests(status, round_no);

CREATE TABLE IF NOT EXISTS knowledge_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cid        TEXT NOT NULL,
    sampled_at REAL NOT NULL,
    sampled_by TEXT NOT NULL DEFAULT '',
    rate       REAL NOT NULL DEFAULT 0.0,
    passed     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_ksamples_cid ON knowledge_samples(cid, sampled_at);
"""


def _ticket_from_row(row: sqlite3.Row) -> QATicket:
    return QATicket(
        ticket_id=row["ticket_id"],
        round_no=row["round_no"],
        location=row["location"],
        option_a=row["option_a"],
        option_b=row["option_b"],
        architect_preference=row["architect_preference"],
        impact=TicketImpact(row["impact"]),
        status=TicketStatus(row["status"]),
        answer=row["answer"],
        created_at=row["created_at"],
    )


def _package_from_payload(round_no: int, payload: str) -> TaskPackage:
    data: dict[str, Any] = json.loads(payload)
    acceptance = [
        AcceptanceCriterion(cid=str(c.get("cid", "")), text=str(c.get("text", "")))
        for c in (data.get("acceptance") or [])
        if isinstance(c, dict)
    ]
    return TaskPackage(
        round_no=round_no,
        feature=str(data.get("feature", "")),
        goal=str(data.get("goal", "")),
        acceptance=acceptance,
        boundaries=[str(x) for x in (data.get("boundaries") or [])],
        interface_contract=[str(x) for x in (data.get("interface_contract") or [])],
        prohibitions=[str(x) for x in (data.get("prohibitions") or [])],
        feedback=[str(x) for x in (data.get("feedback") or [])],
        closed_tickets=[str(x) for x in (data.get("closed_tickets") or [])],
    )


class SqliteStore:
    """SQLite 持久化门面（P0 §3.2 的 18 个签名 + P1 的 7 个；DDL 见 D1-9 / D3 §3.2）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        # Streamlit 等运行环境会换线程复用句柄，故放开线程检查（SQLite 自身仍串行化写）。
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ------------------------------------------------------------ 生命周期

    def init_schema(self) -> None:
        """建库建表（幂等）+ 把 `meta.schema_version` 升到当前版本。

        DDL 全是 `IF NOT EXISTS`，所以 P0 的旧库（schema_version="1"）直接原地升到 "2"：
        建出 ledger / knowledge 两张新表，版本号一并更新，不需要单独迁移脚本。
        """
        self._conn.executescript(SCHEMA_SQL)
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

    def close(self) -> None:
        """关闭连接；已关闭时重复调用是安全的。"""
        try:
            self._conn.close()
        except sqlite3.ProgrammingError:  # pragma: no cover - 重复 close
            pass

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def list_tables(self) -> list[str]:
        """已建表名（含 meta），供自检/验收用。"""
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    # ------------------------------------------------------------ 轮次

    def upsert_round(self, round_: Round) -> Round:
        """写入/更新轮次。**已存在的轮次保留原 state**（改状态请用 `set_round_state`），
        只刷新 design_ref 与 updated_at，避免重跑同轮次时把状态机打回 design_in。"""
        now = time.time()
        existing = self.get_round(round_.round_no)
        created_at = existing.created_at if existing else (round_.created_at or now)
        self._conn.execute(
            """
            INSERT INTO rounds(round_no, design_ref, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(round_no) DO UPDATE SET
                design_ref = excluded.design_ref,
                updated_at = excluded.updated_at
            """,
            (round_.round_no, round_.design_ref, round_.state.value, created_at, now),
        )
        self._conn.commit()
        return self.get_round(round_.round_no)  # type: ignore[return-value]

    def get_round(self, round_no: int) -> Optional[Round]:
        row = self._conn.execute(
            "SELECT * FROM rounds WHERE round_no = ?", (round_no,)
        ).fetchone()
        if row is None:
            return None
        return Round(
            round_no=row["round_no"],
            design_ref=row["design_ref"],
            state=RoundState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def set_round_state(self, round_no: int, state: RoundState) -> None:
        """改轮次状态；轮次不存在时自动补一条骨架记录（design_ref 为空），不抛异常。"""
        now = time.time()
        cur = self._conn.execute(
            "UPDATE rounds SET state = ?, updated_at = ? WHERE round_no = ?",
            (state.value, now, round_no),
        )
        if cur.rowcount == 0:
            self._conn.execute(
                "INSERT INTO rounds(round_no, design_ref, state, created_at, updated_at) VALUES (?, '', ?, ?, ?)",
                (round_no, state.value, now, now),
            )
        self._conn.commit()

    # ------------------------------------------------------------ 質問票

    def add_ticket(self, ticket: QATicket) -> QATicket:
        """新增票；**票号已存在时只刷新票面，保留 status/answer**。

        理由（硬规则相关）：`tickets close` 的人确认结果不能被重跑同轮次覆盖掉，
        否则「关闭高影响票后重跑 round」会永远退化成 tickets_open（D1-1 硬规则）。
        """
        now = time.time()
        existing = self.get_ticket(ticket.ticket_id)
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO tickets(ticket_id, round_no, location, option_a, option_b,
                                    architect_preference, impact, status, answer, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.ticket_id,
                    ticket.round_no,
                    ticket.location,
                    ticket.option_a,
                    ticket.option_b,
                    ticket.architect_preference,
                    ticket.impact.value,
                    ticket.status.value,
                    ticket.answer,
                    ticket.created_at or now,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE tickets SET round_no = ?, location = ?, option_a = ?, option_b = ?,
                                   architect_preference = ?, impact = ?
                 WHERE ticket_id = ?
                """,
                (
                    ticket.round_no,
                    ticket.location,
                    ticket.option_a,
                    ticket.option_b,
                    ticket.architect_preference,
                    ticket.impact.value,
                    ticket.ticket_id,
                ),
            )
        self._conn.commit()
        return self.get_ticket(ticket.ticket_id)  # type: ignore[return-value]

    def get_ticket(self, ticket_id: str) -> Optional[QATicket]:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return None if row is None else _ticket_from_row(row)

    def list_tickets(
        self,
        round_no: Optional[int] = None,
        status: Optional[TicketStatus] = None,
        impact: Optional[TicketImpact] = None,
    ) -> list[QATicket]:
        """按轮次 / 状态 / 影响面过滤（都可为 None = 不过滤），按 round_no、ticket_id 排序。

        D2 R-8：`impact` 下推到 SQL `WHERE`（索引 `ix_tickets_round(round_no, status, impact)` 已覆盖），
        界面不要自己本地过滤。这是方法签名变更，不是数据契约变更（`schemas.py` 不动、不升版本）。
        """
        sql = "SELECT * FROM tickets"
        clauses: list[str] = []
        params: list[Any] = []
        if round_no is not None:
            clauses.append("round_no = ?")
            params.append(round_no)
        if status is not None:
            clauses.append("status = ?")
            params.append(TicketStatus(status).value)
        if impact is not None:
            clauses.append("impact = ?")
            params.append(TicketImpact(impact).value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY round_no, ticket_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [_ticket_from_row(r) for r in rows]

    def close_ticket(self, ticket_id: str, answer: str) -> QATicket:
        """人工确认：写答复并置 closed。票号不存在 → KeyError（中文）。"""
        if self.get_ticket(ticket_id) is None:
            raise KeyError(f"票号不存在：{ticket_id}")
        self._conn.execute(
            "UPDATE tickets SET status = ?, answer = ? WHERE ticket_id = ?",
            (TicketStatus.CLOSED.value, answer, ticket_id),
        )
        self._conn.commit()
        return self.get_ticket(ticket_id)  # type: ignore[return-value]

    def list_open_high_tickets(self, round_no: int) -> list[QATicket]:
        """未关闭的高影响票——非空时**不得**生成任务包（D1-1 硬规则，判定在 pipeline）。"""
        rows = self._conn.execute(
            """
            SELECT * FROM tickets
             WHERE round_no = ? AND impact = ? AND status = ?
             ORDER BY ticket_id
            """,
            (round_no, TicketImpact.HIGH.value, TicketStatus.OPEN.value),
        ).fetchall()
        return [_ticket_from_row(r) for r in rows]

    # ------------------------------------------------------------ 任务包

    def save_package(self, package: TaskPackage) -> str:
        """按轮次保存任务包（同轮次覆盖），返回 `package_hash`。"""
        digest = package_hash(package)
        self._conn.execute(
            """
            INSERT INTO task_packages(round_no, package_hash, payload, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(round_no) DO UPDATE SET
                package_hash = excluded.package_hash,
                payload      = excluded.payload,
                created_at   = excluded.created_at
            """,
            (package.round_no, digest, dumps(package), time.time()),
        )
        self._conn.commit()
        return digest

    def get_package(self, round_no: int) -> Optional[TaskPackage]:
        row = self._conn.execute(
            "SELECT * FROM task_packages WHERE round_no = ?", (round_no,)
        ).fetchone()
        if row is None:
            return None
        return _package_from_payload(row["round_no"], row["payload"])

    # ------------------------------------------------------------ 门禁

    def save_gate_report(self, report: GateReport) -> None:
        """追加一条门禁判定（attempt 之间并存，便于复盘收敛过程）。"""
        self._conn.execute(
            """
            INSERT INTO gate_reports(round_no, attempt, state, reason_key, reasons,
                                     failing_criteria, escalated, same_reason_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.round_no,
                report.attempt,
                report.state.value,
                getattr(report, "reason_key", "") or "",
                dumps(list(report.reasons)),
                dumps(list(report.failing_criteria)),
                bool(report.escalated),
                report.same_reason_count,
                report.created_at or time.time(),
            ),
        )
        self._conn.commit()

    def list_gate_reports(self, round_no: int) -> list[GateReport]:
        rows = self._conn.execute(
            "SELECT * FROM gate_reports WHERE round_no = ? ORDER BY id", (round_no,)
        ).fetchall()
        return [
            GateReport(
                round_no=r["round_no"],
                state=GateState(r["state"]),
                reasons=list(json.loads(r["reasons"])),
                failing_criteria=list(json.loads(r["failing_criteria"])),
                attempt=r["attempt"],
                escalated=bool(r["escalated"]),
                same_reason_count=r["same_reason_count"],
                reason_key=r["reason_key"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------ 失败与回避模式

    def record_failure(self, failure: FailureRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO failures(round_no, attempt, reason, avoid_pattern, reason_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                failure.round_no,
                failure.attempt,
                failure.reason,
                failure.avoid_pattern,
                getattr(failure, "reason_key", "") or "",
                failure.created_at or time.time(),
            ),
        )
        self._conn.commit()

    def list_avoid_patterns(self, round_no: Optional[int] = None) -> list[str]:
        """回避模式清单（去重、去空、字典序）；round_no=None 表示全部轮次。"""
        if round_no is None:
            rows = self._conn.execute(
                "SELECT DISTINCT avoid_pattern FROM failures WHERE avoid_pattern <> '' ORDER BY avoid_pattern"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT avoid_pattern FROM failures WHERE avoid_pattern <> '' AND round_no = ? ORDER BY avoid_pattern",
                (round_no,),
            ).fetchall()
        return [r["avoid_pattern"] for r in rows]

    def count_same_failure(self, round_no: int, reason_key: str) -> int:
        """同类失败计数（D1-4 / D1-5 判据 G5）。

        - 匹配口径：先按 `failures.reason_key`（D1-4 归一化键，`sha1(category|target)[:12]`）匹配；
          历史数据如果没写 reason_key（空串），退化为按 `reason` 精确匹配——兼容长流早期口径。
        - **返回值是库里已有的条数**；D1-4 定义 `same_reason_count`（不含本次）应在**写入本次
          FailureRecord 之前**调用，然后 `+1 >= 2` 判升级；若调用方习惯「先写后数」，则判 `>= 2`，
          两种顺序等价，取哪个数写进 GateReport 由 gates 按 D1-4 决定。
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM failures WHERE round_no = ? AND (reason_key = ? OR reason = ?)",
            (round_no, reason_key, reason_key),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ 快照

    def save_snapshot(self, snapshot: Snapshot) -> None:
        """每轮一条快照（同轮次覆盖）。D1-6：只有 PASS 轮带 tag，由 pipeline 决定。"""
        self._conn.execute(
            """
            INSERT INTO snapshots(round_no, package_hash, gate_state, git_sha, tag, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(round_no) DO UPDATE SET
                package_hash = excluded.package_hash,
                gate_state   = excluded.gate_state,
                git_sha      = excluded.git_sha,
                tag          = excluded.tag,
                created_at   = excluded.created_at
            """,
            (
                snapshot.round_no,
                snapshot.package_hash,
                snapshot.gate_state.value,
                snapshot.git_sha,
                snapshot.tag,
                snapshot.created_at or time.time(),
            ),
        )
        self._conn.commit()

    def get_snapshot(self, round_no: int) -> Optional[Snapshot]:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE round_no = ?", (round_no,)
        ).fetchone()
        if row is None:
            return None
        return Snapshot(
            round_no=row["round_no"],
            package_hash=row["package_hash"],
            gate_state=GateState(row["gate_state"]),
            git_sha=row["git_sha"],
            tag=row["tag"],
            created_at=row["created_at"],
        )

    def list_snapshots(self) -> list[Snapshot]:
        rows = self._conn.execute("SELECT * FROM snapshots ORDER BY round_no").fetchall()
        return [
            Snapshot(
                round_no=r["round_no"],
                package_hash=r["package_hash"],
                gate_state=GateState(r["gate_state"]),
                git_sha=r["git_sha"],
                tag=r["tag"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------ P1 token 账本（D3 §3.2 / §3.4）

    def add_ledger_entry(self, entry: LedgerEntry) -> None:
        """追加一条 token 账目（只落库，不做任何统计/判定）。"""
        self._conn.execute(
            """
            INSERT INTO ledger(round_no, role, model_ref, tokens_in, tokens_out,
                               cost, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.round_no,
                entry.role,
                entry.model_ref,
                int(entry.tokens_in),
                int(entry.tokens_out),
                float(entry.cost),
                entry.source,
                entry.created_at or time.time(),
            ),
        )
        self._conn.commit()

    def list_ledger(self, round_no: Optional[int] = None) -> list[LedgerEntry]:
        """按轮次列账目（None = 全部），按 round_no、id 排序。"""
        if round_no is None:
            rows = self._conn.execute("SELECT * FROM ledger ORDER BY round_no, id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM ledger WHERE round_no = ? ORDER BY id", (round_no,)
            ).fetchall()
        return [
            LedgerEntry(
                round_no=r["round_no"],
                role=r["role"],
                model_ref=r["model_ref"],
                tokens_in=r["tokens_in"],
                tokens_out=r["tokens_out"],
                cost=r["cost"],
                source=r["source"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------ P1 知识沉淀（D3 §3.2 / §3.5）

    def add_candidate(self, c: KnowledgeCandidate) -> None:
        """新增沉淀候选；同 cid 覆盖（幂等重跑）。"""
        self._conn.execute(
            """
            INSERT INTO knowledge(cid, round_no, kind, content, confidence, status, reused_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cid) DO UPDATE SET
                round_no   = excluded.round_no,
                kind       = excluded.kind,
                content    = excluded.content,
                confidence = excluded.confidence,
                status     = excluded.status,
                reused_by  = excluded.reused_by,
                created_at = excluded.created_at
            """,
            (
                c.cid,
                c.round_no,
                c.kind,
                c.content,
                float(c.confidence),
                _enum_value(c.status),
                dumps(list(c.reused_by)),
                c.created_at or time.time(),
            ),
        )
        self._conn.commit()

    def get_candidate(self, cid: str) -> Optional[KnowledgeCandidate]:
        row = self._conn.execute("SELECT * FROM knowledge WHERE cid = ?", (cid,)).fetchone()
        return None if row is None else _candidate_from_row(row)

    def list_candidates(self, status: Optional[str] = None) -> list[KnowledgeCandidate]:
        """按状态列候选（None = 全部），按 created_at、cid 排序。

        `status` 接受裸字符串或 `CandidateStatus`（str Enum），两者等价。
        """
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM knowledge ORDER BY created_at, cid"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge WHERE status = ? ORDER BY created_at, cid",
                (_enum_value(status),),
            ).fetchall()
        return [_candidate_from_row(r) for r in rows]

    def promote_candidate(self, cid: str, reused_by_round: int) -> KnowledgeCandidate:
        """转正：追加一次成功轮复用并置 `status=promoted`。cid 不存在 → KeyError（中文）。"""
        candidate = self.get_candidate(cid)
        if candidate is None:
            raise KeyError(f"沉淀候选不存在：{cid}")
        reused = list(candidate.reused_by)
        if reused_by_round not in reused:
            reused.append(reused_by_round)
        self._conn.execute(
            "UPDATE knowledge SET status = ?, reused_by = ? WHERE cid = ?",
            ("promoted", dumps(reused), cid),
        )
        self._conn.commit()
        return self.get_candidate(cid)  # type: ignore[return-value]

    def drop_candidate(self, cid: str, reason: str) -> None:
        """丢弃候选（`status=dropped`）；cid 不存在 → KeyError（中文）。

        `reason` 只进调用方日志，P0/P1 的 knowledge 表不存该列（DDL 见 D3 未定义），
        避免为了日志字段擅自扩表——需要落库时请先让 Chief 裁。
        """
        if self.get_candidate(cid) is None:
            raise KeyError(f"沉淀候选不存在：{cid}")
        self._conn.execute("UPDATE knowledge SET status = ? WHERE cid = ?", ("dropped", cid))
        self._conn.commit()

    # ------------------------------------------------------------ P2 评审留痕（D4-3.3 / A19）

    def add_review_report(self, report: ReviewReport) -> None:
        """落一条评审留痕（含 `context_id`，A19 靠它验 fresh context）。"""
        self._conn.execute(
            """
            INSERT INTO reviews(round_no, attempt, provider, model_ref, context_id,
                                parse_ok, verdict, findings, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.round_no,
                report.attempt,
                report.provider,
                report.model_ref,
                report.context_id,
                1 if report.parse_ok else 0,
                report.verdict,
                dumps(list(report.findings)),
                report.created_at or time.time(),
            ),
        )
        self._conn.commit()

    def list_review_reports(self, round_no: Optional[int] = None) -> list[ReviewReport]:
        """按轮次列评审留痕（None = 全部），按 round_no、attempt、id 排序。"""
        if round_no is None:
            rows = self._conn.execute(
                "SELECT * FROM reviews ORDER BY round_no, attempt, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM reviews WHERE round_no = ? ORDER BY attempt, id", (round_no,)
            ).fetchall()
        return [_review_from_row(r) for r in rows]

    # ------------------------------------------------------------ P3 快车道落库（D5-3.2 / A23）

    def add_fastlane_request(self, request: FastlaneRequest) -> FastlaneRequest:
        """登记/更新一条快车道请求（幂等：同 `request_id` 覆盖）。

        **只在既有行仍是 `pending` 时覆盖**（`ON CONFLICT ... DO UPDATE ... WHERE`）：
        已决行（approved/rejected/escalated）的 `status/batch_id/decided_by/decided_at` 是审计留痕，
        重跑 `plan` 不许把它们改回 pending，也不许抹掉承认人。真要翻案必须新开请求（D5-3.2）。
        """
        self._conn.execute(
            """
            INSERT INTO fastlane_requests(request_id, round_no, summary, diff_files,
                                         new_interface, new_data_model, touched_modules,
                                         status, batch_id, decided_by, decided_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                round_no        = excluded.round_no,
                summary         = excluded.summary,
                diff_files      = excluded.diff_files,
                new_interface   = excluded.new_interface,
                new_data_model  = excluded.new_data_model,
                touched_modules = excluded.touched_modules,
                status          = excluded.status,
                batch_id        = excluded.batch_id,
                decided_by      = excluded.decided_by,
                decided_at      = excluded.decided_at,
                note            = excluded.note
            WHERE fastlane_requests.status = 'pending'
            """,
            (
                request.request_id,
                request.round_no,
                request.summary,
                dumps(list(request.diff_files)),
                1 if request.new_interface else 0,
                1 if request.new_data_model else 0,
                dumps(list(request.touched_modules)),
                _enum_value(request.status),
                request.batch_id,
                request.decided_by,
                float(request.decided_at or 0.0),
                request.note,
            ),
        )
        self._conn.commit()
        return self.get_fastlane_request(request.request_id)  # type: ignore[return-value]

    def get_fastlane_request(self, request_id: str) -> Optional[FastlaneRequest]:
        row = self._conn.execute(
            "SELECT * FROM fastlane_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else _fastlane_from_row(row)

    def list_fastlane_requests(
        self,
        status: Optional[FastlaneStatus | str] = None,
        round_no: Optional[int] = None,
    ) -> list[FastlaneRequest]:
        """列快车道请求（两个过滤条件可选），按 `round_no, request_id` 稳定排序。

        `status` 接受 `FastlaneStatus` 或裸字符串（str Enum，两者等价）。
        """
        sql = "SELECT * FROM fastlane_requests"
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(_enum_value(status))
        if round_no is not None:
            clauses.append("round_no = ?")
            params.append(round_no)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY round_no, request_id"
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_fastlane_from_row(r) for r in rows]

    def ack_fastlane_request(
        self,
        request_id: str,
        by: str = "",
        status: FastlaneStatus | str = FastlaneStatus.APPROVED,
        batch_id: str = "",
        note: str = "",
    ) -> FastlaneRequest:
        """承认/拒绝一条快车道请求，返回落库后的记录（D5-3.2）。

        幂等与不可翻案（A23 的三条硬断言都落在这一处，两驱动同签名同语义）：
        - `request_id` 不存在 → `KeyError`（中文），不静默造行；
        - **已决行**（approved/rejected/escalated）二次承认**不写行、不覆盖**
          `decided_by/decided_at/status`，原样返回首次记录 —— 因此
          `ESCALATED`/`REJECTED` **不可能**被 `ack` 翻成 `APPROVED`；
        - 只有 `pending` 行会被写入：`status`、`decided_by`、`decided_at`（现在）、
          `batch_id`（攒批承认同批共享）、`note`（空则保留原值）。
        """
        current = self.get_fastlane_request(request_id)
        if current is None:
            raise KeyError(f"快车道请求不存在：{request_id}")
        if _enum_value(current.status) != FastlaneStatus.PENDING.value:
            return current  # 已决 → 幂等返回，审计字段一个都不动
        self._conn.execute(
            "UPDATE fastlane_requests SET status = ?, batch_id = ?, decided_by = ?, "
            "decided_at = ?, note = ? WHERE request_id = ? AND status = ?",
            (
                _enum_value(status),
                batch_id,
                by,
                time.time(),
                note or current.note,
                request_id,
                FastlaneStatus.PENDING.value,
            ),
        )
        self._conn.commit()
        return self.get_fastlane_request(request_id)  # type: ignore[return-value]

    # ------------------------------------------------------------ P3 抽检留痕（D5-3.3 / A25）

    def add_knowledge_sample(self, sample: KnowledgeSample) -> KnowledgeSample:
        """落一条人工抽检留痕（**append-only**：一行一次抽检结论，同 cid 可多行）。

        `sampled_at` 为 0 时取当前时间；`passed` 转 0/1 入库（两驱动同构）。
        """
        sampled_at = float(sample.sampled_at or time.time())
        cur = self._conn.execute(
            "INSERT INTO knowledge_samples(cid, sampled_at, sampled_by, rate, passed) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sample.cid,
                sampled_at,
                sample.sampled_by,
                float(sample.rate),
                1 if sample.passed else 0,
            ),
        )
        self._conn.commit()
        return _sample_from_row(
            self._conn.execute(
                "SELECT * FROM knowledge_samples WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        )

    def list_knowledge_samples(self, cid: Optional[str] = None) -> list[KnowledgeSample]:
        """按 cid 列抽检留痕（None = 全部），按 `sampled_at, id` 稳定排序（审计可回放）。"""
        if cid is None:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_samples ORDER BY sampled_at, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_samples WHERE cid = ? ORDER BY sampled_at, id", (cid,)
            ).fetchall()
        return [_sample_from_row(r) for r in rows]


def _fastlane_from_row(row: sqlite3.Row) -> FastlaneRequest:
    """把 fastlane_requests 行还原成 `FastlaneRequest`（列表走 JSON，布尔还原）."""
    return FastlaneRequest(
        request_id=row["request_id"],
        round_no=row["round_no"],
        summary=row["summary"],
        diff_files=_json_list(row["diff_files"]),
        new_interface=bool(row["new_interface"]),
        new_data_model=bool(row["new_data_model"]),
        touched_modules=_json_list(row["touched_modules"]),
        status=FastlaneStatus(row["status"]),
        batch_id=row["batch_id"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        note=row["note"],
    )


def _json_list(raw: object) -> list[str]:
    """TEXT 列里的 JSON 列表 → `list[str]`（坏数据不炸读路径，退化成空列表）。"""
    try:
        parsed = json.loads(str(raw)) if raw else []
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def _sample_from_row(row: sqlite3.Row) -> KnowledgeSample:
    return KnowledgeSample(
        cid=row["cid"],
        sampled_at=row["sampled_at"],
        sampled_by=row["sampled_by"],
        rate=row["rate"],
        passed=bool(row["passed"]),
    )


def _severity_of(raw: object) -> Severity:
    """评审 severity 宽容解析：未知/缺失一律归 INFO（D4-3.4「宽容解析」，不因格式差异炸门禁）。"""
    try:
        return Severity(str(raw))
    except ValueError:
        return Severity.INFO


def _review_from_row(row: sqlite3.Row) -> ReviewReport:
    """把 reviews 行还原成 `ReviewReport`（findings 走 JSON，severity 还原成枚举）。"""
    try:
        raw = json.loads(row["findings"]) if row["findings"] else []
    except (TypeError, ValueError):
        raw = []
    findings = [
        ReviewFinding(
            severity=_severity_of(item.get("severity", Severity.INFO.value)),
            location=str(item.get("location", "")),
            issue=str(item.get("issue", "")),
            suggestion=str(item.get("suggestion", "")),
            criterion=str(item.get("criterion", "")),
        )
        for item in raw
        if isinstance(item, dict)
    ]
    return ReviewReport(
        round_no=row["round_no"],
        provider=row["provider"],
        model_ref=row["model_ref"],
        context_id=row["context_id"],
        parse_ok=bool(row["parse_ok"]),
        verdict=row["verdict"],
        findings=findings,
        attempt=row["attempt"],
        created_at=row["created_at"],
    )


def _candidate_from_row(row: sqlite3.Row) -> KnowledgeCandidate:
    raw = row["reused_by"]
    try:
        reused = [int(x) for x in json.loads(raw)] if raw else []
    except (TypeError, ValueError):
        reused = []
    return KnowledgeCandidate(
        cid=row["cid"],
        round_no=row["round_no"],
        kind=row["kind"],
        content=row["content"],
        confidence=row["confidence"],
        status=CandidateStatus(row["status"]),
        reused_by=reused,
        created_at=row["created_at"],
    )


__all__ = [
    "SqliteStore",
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
    "schemas",
]

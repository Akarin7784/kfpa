"""PostgreSQL 驱动（docs/dev/p1-plan.md §3.2 / D3）。

- 与 :class:`~kfpa.store.sqlite.SqliteStore` **方法签名与行为一一对应**（A12 双库对拍的基础）；
- 惰性 import `psycopg`：缺包 → 中文 `RuntimeError`（CLI 归退出码 4），P0 离线路径不受影响；
- DDL 只来自 `migrations/0001_init.sql`（单一事实来源），本文件不复制建表语句；
- 时间戳一律 epoch 秒（DOUBLE PRECISION），JSON/列表存 TEXT（与 SQLite 同构，保证对拍语义一致）；
- 日志/异常里的 DSN 一律走 :func:`kfpa.store.mask_dsn`，密码不得泄漏；
- 连接用 `autocommit=True`：与 SQLite 侧「每写立即生效」的语义对齐。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

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
from . import mask_dsn, migrations_dir
from .sqlite import SCHEMA_VERSION, _enum_value, _severity_of


class PostgresStore:
    """psycopg3 驱动的 Store 实现（方法集见 :class:`kfpa.store.StoreProtocol`）。"""

    name = "postgres"

    def __init__(self, dsn: str) -> None:
        self.dsn_masked = mask_dsn(dsn)
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "未安装 psycopg：这是 P1 依赖缺失 → 请让 Chief 统一安装（pip install 'psycopg[binary]'），"
                "或改用 SQLite（不设 KFPA_DB_URL）"
            ) from exc

        try:
            self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        except Exception as exc:  # 连接失败要给中文提示，且不许泄漏密码
            raise RuntimeError(
                f"PostgreSQL 连接失败（{self.dsn_masked}）：{type(exc).__name__}: {exc} → "
                "先 `docker compose -f docker-compose.pg.yml up -d` 并等健康检查通过"
            ) from exc
        self.db_path = self.dsn_masked
        self.init_schema()

    # ------------------------------------------------------------ 生命周期

    def init_schema(self) -> None:
        """按文件名顺序执行 `migrations/*.sql`（幂等）+ 写 `meta.schema_version`。

        P2 起 `0001_init.sql` / `0002_reviews.sql` 依次应用；新增迁移只要加文件。
        """
        paths = sorted(migrations_dir().glob("*.sql"))
        if not paths:
            raise RuntimeError(
                f"找不到迁移文件：{migrations_dir()} → 检查仓库是否完整（migrations/0001_init.sql）"
            )
        with self._conn.cursor() as cur:
            for path in paths:
                for statement in _split_statements(path.read_text(encoding="utf-8")):
                    cur.execute(statement)
            cur.execute(
                "INSERT INTO meta(key, value) VALUES(%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                ("schema_version", SCHEMA_VERSION),
            )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - 重复 close
            pass

    def __enter__(self) -> "PostgresStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def list_tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        ).fetchall()
        return [r["table_name"] for r in rows]

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(%s, %s) "
            "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
            (key, str(value)),
        )

    # ------------------------------------------------------------ 轮次

    def upsert_round(self, round_: Round) -> Round:
        existing = self.get_round(round_.round_no)
        created_at = existing.created_at if existing else (round_.created_at or time.time())
        self._conn.execute(
            """
            INSERT INTO rounds(round_no, design_ref, state, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(round_no) DO UPDATE SET
                design_ref = EXCLUDED.design_ref,
                updated_at = EXCLUDED.updated_at
            """,
            (round_.round_no, round_.design_ref, round_.state.value, created_at, time.time()),
        )
        return self.get_round(round_.round_no)  # type: ignore[return-value]

    def get_round(self, round_no: int) -> Optional[Round]:
        row = self._conn.execute("SELECT * FROM rounds WHERE round_no = %s", (round_no,)).fetchone()
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
        now = time.time()
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE rounds SET state = %s, updated_at = %s WHERE round_no = %s",
                (state.value, now, round_no),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO rounds(round_no, design_ref, state, created_at, updated_at) "
                    "VALUES (%s, '', %s, %s, %s)",
                    (round_no, state.value, now, now),
                )

    # ------------------------------------------------------------ 質問票

    def add_ticket(self, ticket: QATicket) -> QATicket:
        existing = self.get_ticket(ticket.ticket_id)
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO tickets(ticket_id, round_no, location, option_a, option_b,
                                    architect_preference, impact, status, answer, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    ticket.created_at or time.time(),
                ),
            )
        else:
            # 与 SQLite 完全一致：只刷新票面，保留 status/answer（R-6）
            self._conn.execute(
                """
                UPDATE tickets SET round_no = %s, location = %s, option_a = %s, option_b = %s,
                                   architect_preference = %s, impact = %s
                 WHERE ticket_id = %s
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
        return self.get_ticket(ticket.ticket_id)  # type: ignore[return-value]

    def get_ticket(self, ticket_id: str) -> Optional[QATicket]:
        row = self._conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = %s", (ticket_id,)
        ).fetchone()
        return None if row is None else _ticket_from_row(row)

    def list_tickets(
        self,
        round_no: Optional[int] = None,
        status: Optional[TicketStatus] = None,
        impact: Optional[TicketImpact] = None,
    ) -> list[QATicket]:
        clauses: list[str] = []
        params: list[Any] = []
        if round_no is not None:
            clauses.append("round_no = %s")
            params.append(round_no)
        if status is not None:
            clauses.append("status = %s")
            params.append(TicketStatus(status).value)
        if impact is not None:
            clauses.append("impact = %s")
            params.append(TicketImpact(impact).value)
        sql = "SELECT * FROM tickets"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY round_no, ticket_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [_ticket_from_row(r) for r in rows]

    def close_ticket(self, ticket_id: str, answer: str) -> QATicket:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status = %s, answer = %s WHERE ticket_id = %s",
                (TicketStatus.CLOSED.value, answer, ticket_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"票号不存在：{ticket_id}")
        return self.get_ticket(ticket_id)  # type: ignore[return-value]

    def list_open_high_tickets(self, round_no: int) -> list[QATicket]:
        rows = self._conn.execute(
            """
            SELECT * FROM tickets
             WHERE round_no = %s AND impact = %s AND status = %s
             ORDER BY ticket_id
            """,
            (round_no, TicketImpact.HIGH.value, TicketStatus.OPEN.value),
        ).fetchall()
        return [_ticket_from_row(r) for r in rows]

    # ------------------------------------------------------------ 任务包

    def save_package(self, package: TaskPackage) -> str:
        digest = package_hash(package)
        self._conn.execute(
            """
            INSERT INTO task_packages(round_no, package_hash, payload, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(round_no) DO UPDATE SET
                package_hash = EXCLUDED.package_hash,
                payload      = EXCLUDED.payload,
                created_at   = EXCLUDED.created_at
            """,
            (package.round_no, digest, dumps(package), time.time()),
        )
        return digest

    def get_package(self, round_no: int) -> Optional[TaskPackage]:
        row = self._conn.execute(
            "SELECT * FROM task_packages WHERE round_no = %s", (round_no,)
        ).fetchone()
        if row is None:
            return None
        return _package_from_payload(row["round_no"], row["payload"])

    # ------------------------------------------------------------ 门禁

    def save_gate_report(self, report: GateReport) -> None:
        self._conn.execute(
            """
            INSERT INTO gate_reports(round_no, attempt, state, reason_key, reasons,
                                     failing_criteria, escalated, same_reason_count, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    def list_gate_reports(self, round_no: int) -> list[GateReport]:
        rows = self._conn.execute(
            "SELECT * FROM gate_reports WHERE round_no = %s ORDER BY id", (round_no,)
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
            VALUES (%s, %s, %s, %s, %s, %s)
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

    def list_avoid_patterns(self, round_no: Optional[int] = None) -> list[str]:
        if round_no is None:
            rows = self._conn.execute(
                "SELECT DISTINCT avoid_pattern FROM failures WHERE avoid_pattern <> '' "
                "ORDER BY avoid_pattern"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT avoid_pattern FROM failures "
                "WHERE avoid_pattern <> '' AND round_no = %s ORDER BY avoid_pattern",
                (round_no,),
            ).fetchall()
        return [r["avoid_pattern"] for r in rows]

    def count_same_failure(self, round_no: int, reason_key: str) -> int:
        """与 SQLite 同口径（D2 R-4）：先按 reason_key，空 key 退化为 reason 精确匹配。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM failures "
            "WHERE round_no = %s AND (reason_key = %s OR reason = %s)",
            (round_no, reason_key, reason_key),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ 快照

    def save_snapshot(self, snapshot: Snapshot) -> None:
        self._conn.execute(
            """
            INSERT INTO snapshots(round_no, package_hash, gate_state, git_sha, tag, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(round_no) DO UPDATE SET
                package_hash = EXCLUDED.package_hash,
                gate_state   = EXCLUDED.gate_state,
                git_sha      = EXCLUDED.git_sha,
                tag          = EXCLUDED.tag,
                created_at   = EXCLUDED.created_at
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

    def get_snapshot(self, round_no: int) -> Optional[Snapshot]:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE round_no = %s", (round_no,)
        ).fetchone()
        if row is None:
            return None
        return _snapshot_from_row(row)

    def list_snapshots(self) -> list[Snapshot]:
        rows = self._conn.execute("SELECT * FROM snapshots ORDER BY round_no").fetchall()
        return [_snapshot_from_row(r) for r in rows]

    # ------------------------------------------------------------ P1 token 账本

    def add_ledger_entry(self, entry: LedgerEntry) -> None:
        self._conn.execute(
            """
            INSERT INTO ledger(round_no, role, model_ref, tokens_in, tokens_out,
                               cost, source, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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

    def list_ledger(self, round_no: Optional[int] = None) -> list[LedgerEntry]:
        if round_no is None:
            rows = self._conn.execute("SELECT * FROM ledger ORDER BY round_no, id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM ledger WHERE round_no = %s ORDER BY id", (round_no,)
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

    # ------------------------------------------------------------ P1 知识沉淀

    def add_candidate(self, c: KnowledgeCandidate) -> None:
        self._conn.execute(
            """
            INSERT INTO knowledge(cid, round_no, kind, content, confidence, status, reused_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(cid) DO UPDATE SET
                round_no   = EXCLUDED.round_no,
                kind       = EXCLUDED.kind,
                content    = EXCLUDED.content,
                confidence = EXCLUDED.confidence,
                status     = EXCLUDED.status,
                reused_by  = EXCLUDED.reused_by,
                created_at = EXCLUDED.created_at
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

    def get_candidate(self, cid: str) -> Optional[KnowledgeCandidate]:
        row = self._conn.execute("SELECT * FROM knowledge WHERE cid = %s", (cid,)).fetchone()
        return None if row is None else _candidate_from_row(row)

    def list_candidates(self, status: Optional[str] = None) -> list[KnowledgeCandidate]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM knowledge ORDER BY created_at, cid"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge WHERE status = %s ORDER BY created_at, cid",
                (_enum_value(status),),
            ).fetchall()
        return [_candidate_from_row(r) for r in rows]

    def promote_candidate(self, cid: str, reused_by_round: int) -> KnowledgeCandidate:
        candidate = self.get_candidate(cid)
        if candidate is None:
            raise KeyError(f"沉淀候选不存在：{cid}")
        reused = list(candidate.reused_by)
        if reused_by_round not in reused:
            reused.append(reused_by_round)
        self._conn.execute(
            "UPDATE knowledge SET status = %s, reused_by = %s WHERE cid = %s",
            ("promoted", dumps(reused), cid),
        )
        return self.get_candidate(cid)  # type: ignore[return-value]

    def drop_candidate(self, cid: str, reason: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UPDATE knowledge SET status = %s WHERE cid = %s", ("dropped", cid))
            if cur.rowcount == 0:
                raise KeyError(f"沉淀候选不存在：{cid}")

    # ------------------------------------------------------------ P2 评审留痕（D4-3.3 / A19）

    def add_review_report(self, report: ReviewReport) -> None:
        self._conn.execute(
            """
            INSERT INTO reviews(round_no, attempt, provider, model_ref, context_id,
                                parse_ok, verdict, findings, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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

    def list_review_reports(self, round_no: Optional[int] = None) -> list[ReviewReport]:
        if round_no is None:
            rows = self._conn.execute(
                "SELECT * FROM reviews ORDER BY round_no, attempt, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM reviews WHERE round_no = %s ORDER BY attempt, id", (round_no,)
            ).fetchall()
        return [_review_from_row(r) for r in rows]

    # ------------------------------------------------------------ P3 快车道落库（D5-3.2 / A23）

    def add_fastlane_request(self, request: FastlaneRequest) -> FastlaneRequest:
        """登记/更新一条快车道请求（同 SQLite 侧语义：只在既有行仍 pending 时覆盖）。"""
        self._conn.execute(
            """
            INSERT INTO fastlane_requests(request_id, round_no, summary, diff_files,
                                         new_interface, new_data_model, touched_modules,
                                         status, batch_id, decided_by, decided_at, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(request_id) DO UPDATE SET
                round_no        = EXCLUDED.round_no,
                summary         = EXCLUDED.summary,
                diff_files      = EXCLUDED.diff_files,
                new_interface   = EXCLUDED.new_interface,
                new_data_model  = EXCLUDED.new_data_model,
                touched_modules = EXCLUDED.touched_modules,
                status          = EXCLUDED.status,
                batch_id        = EXCLUDED.batch_id,
                decided_by      = EXCLUDED.decided_by,
                decided_at      = EXCLUDED.decided_at,
                note            = EXCLUDED.note
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
        return self.get_fastlane_request(request.request_id)  # type: ignore[return-value]

    def get_fastlane_request(self, request_id: str) -> Optional[FastlaneRequest]:
        row = self._conn.execute(
            "SELECT * FROM fastlane_requests WHERE request_id = %s", (request_id,)
        ).fetchone()
        return None if row is None else _fastlane_from_row(row)

    def update_fastlane_request(self, request: FastlaneRequest) -> FastlaneRequest:
        """整对象写回（同 SQLite 侧语义：审计优先，已决行不覆盖、不翻案）。"""
        current = self.get_fastlane_request(request.request_id)
        if current is not None and _enum_value(current.status) != FastlaneStatus.PENDING.value:
            return current
        self._conn.execute(
            """
            INSERT INTO fastlane_requests(request_id, round_no, summary, diff_files,
                                         new_interface, new_data_model, touched_modules,
                                         status, batch_id, decided_by, decided_at, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(request_id) DO UPDATE SET
                round_no        = EXCLUDED.round_no,
                summary         = EXCLUDED.summary,
                diff_files      = EXCLUDED.diff_files,
                new_interface   = EXCLUDED.new_interface,
                new_data_model  = EXCLUDED.new_data_model,
                touched_modules = EXCLUDED.touched_modules,
                status          = EXCLUDED.status,
                batch_id        = EXCLUDED.batch_id,
                decided_by      = EXCLUDED.decided_by,
                decided_at      = EXCLUDED.decided_at,
                note            = EXCLUDED.note
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
        return self.get_fastlane_request(request.request_id)  # type: ignore[return-value]

    def list_fastlane_requests(
        self,
        status: Optional[FastlaneStatus | str] = None,
        round_no: Optional[int] = None,
    ) -> list[FastlaneRequest]:
        sql = "SELECT * FROM fastlane_requests"
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(_enum_value(status))
        if round_no is not None:
            clauses.append("round_no = %s")
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
        """承认/拒绝一条快车道请求（幂等；已决行原样返回，不覆盖审计字段）。"""
        current = self.get_fastlane_request(request_id)
        if current is None:
            raise KeyError(f"快车道请求不存在：{request_id}")
        if _enum_value(current.status) != FastlaneStatus.PENDING.value:
            return current
        self._conn.execute(
            "UPDATE fastlane_requests SET status = %s, batch_id = %s, decided_by = %s, "
            "decided_at = %s, note = %s WHERE request_id = %s AND status = %s",
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
        return self.get_fastlane_request(request_id)  # type: ignore[return-value]

    # ------------------------------------------------------------ P3 抽检留痕（D5-3.3 / A25）

    def add_knowledge_sample(self, sample: KnowledgeSample) -> KnowledgeSample:
        """落一条人工抽检留痕（append-only，同 SQLite 侧）。"""
        sampled_at = float(sample.sampled_at or time.time())
        row = self._conn.execute(
            "INSERT INTO knowledge_samples(cid, sampled_at, sampled_by, rate, passed) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (
                sample.cid,
                sampled_at,
                sample.sampled_by,
                float(sample.rate),
                1 if sample.passed else 0,
            ),
        ).fetchone()
        return _sample_from_row(row)

    def list_knowledge_samples(self, cid: Optional[str] = None) -> list[KnowledgeSample]:
        if cid is None:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_samples ORDER BY sampled_at, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_samples WHERE cid = %s ORDER BY sampled_at, id", (cid,)
            ).fetchall()
        return [_sample_from_row(r) for r in rows]


# ---------------------------------------------------------------- 内部工具


def _fastlane_from_row(row: dict[str, Any]) -> FastlaneRequest:
    """把 fastlane_requests 行还原成 `FastlaneRequest`（与 SQLite 侧同一口径）。"""
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
    try:
        parsed = json.loads(str(raw)) if raw else []
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def _sample_from_row(row: dict[str, Any]) -> KnowledgeSample:
    return KnowledgeSample(
        cid=row["cid"],
        sampled_at=row["sampled_at"],
        sampled_by=row["sampled_by"],
        rate=row["rate"],
        passed=bool(row["passed"]),
    )


def _review_from_row(row: dict[str, Any]) -> ReviewReport:
    """把 reviews 行还原成 `ReviewReport`（与 SQLite 侧同一口径）。"""
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


def _split_statements(script: str) -> list[str]:
    """把迁移脚本拆成单条语句（psycopg 的多语句执行随版本而异，拆开最稳）。

    本项目的迁移脚本里不含函数体、美元引用或含分号的字符串字面量，按 `;` 拆分是安全的；
    若将来出现这类语句，请改用 psycopg 的 sql 模块，不要在这里加特例。
    """
    return [stmt.strip() for stmt in script.split(";") if stmt.strip()]


def _ticket_from_row(row: dict[str, Any]) -> QATicket:
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


def _snapshot_from_row(row: dict[str, Any]) -> Snapshot:
    return Snapshot(
        round_no=row["round_no"],
        package_hash=row["package_hash"],
        gate_state=GateState(row["gate_state"]),
        git_sha=row["git_sha"],
        tag=row["tag"],
        created_at=row["created_at"],
    )


def _candidate_from_row(row: dict[str, Any]) -> KnowledgeCandidate:
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


__all__ = ["PostgresStore"]


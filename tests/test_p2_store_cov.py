"""R-30 交接改写：`store/__init__.py` / `store/sqlite.py` 逐分支 + R-31 rc 三分（镜鉴独立复核版）。

来源：磐石 `benchmarks/p2_cov_store/stub_store_coverage.py`（`p2/cov-store` / #48）。
**改写口径（R-30）**：

1. 层级假设改掉：原桩用 `parents[2]` 定位仓库根，搬进 `tests/` 后是 `parents[1]`，
   且 `conftest.py` 已把 `src/` 挂进 `sys.path`，不再自己动 `sys.path`（界面刚在层级上栽过一次）；
2. `postgresql://` 那条**不依赖「PG 驱动已装」**：无论走到「连接失败」还是「未安装 psycopg」，
   都必须是 `RuntimeError` 且**消息里不许出现密码明文**——这才是 `__init__` 这段的职责；
3. 补了几条实现方桩没写的：`list_candidates` 的三种筛选形态、`count_same_failure` 在
   `reason_key` 为空时退化按 `reason`、`make_store` 的显式路径压过环境变量（R-25.1 正反两面）。

`store/postgres.py` 按 R-32 不在 A20 门槛内（由 A12 双库对拍保证），这里不碰。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

import kfpa.cli as cli
import kfpa.pipeline as pipeline
import kfpa.store as st
from kfpa.schemas import (AcceptanceCriterion, CandidateStatus, FailureRecord, GateReport,
                          GateState, KnowledgeCandidate, LedgerEntry, QATicket, ReviewFinding,
                          ReviewReport, Round, RoundState, Severity, Snapshot, TaskPackage,
                          TicketImpact, TicketStatus)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A20 是 clean env：本机残留 `KFPA_DB_URL` 不许污染这些用例（R-25.3 同理）。"""
    monkeypatch.delenv(st.DB_URL_ENV, raising=False)


# ---------------------------------------------------------------- store/__init__.py


def test_make_store_accepts_memory_dsn_and_plain_path(tmp_path):
    assert st.make_store(":memory:").db_path == ":memory:"
    assert st.make_store(f"sqlite://{tmp_path / 'a.db'}").db_path.endswith("a.db")
    assert st.make_store(str(tmp_path / "b.db")).db_path.endswith("b.db")


def test_make_store_default_and_literal_default_both_defer_to_env(tmp_path, monkeypatch):
    """R-25.1：显式路径压过环境变量；**默认值**（无参或字面量 `kfpa.db`）让给环境变量。"""
    env_db = tmp_path / "env.db"
    monkeypatch.setenv(st.DB_URL_ENV, f"sqlite://{env_db}")
    assert st.make_store("kfpa.db").db_path.endswith("env.db")
    assert st.make_store().db_path.endswith("env.db")
    assert st.make_store(str(tmp_path / "explicit.db")).db_path.endswith("explicit.db")
    monkeypatch.delenv(st.DB_URL_ENV, raising=False)
    assert st.make_store("").db_path == st.DEFAULT_SQLITE_PATH
    assert st.make_store().db_path == st.DEFAULT_SQLITE_PATH


def test_make_store_default_in_empty_cwd_uses_default_path():
    with tempfile.TemporaryDirectory() as td:
        cwd = os.getcwd()
        os.chdir(td)
        try:
            store = st.make_store()
            assert store.db_path == st.DEFAULT_SQLITE_PATH
            store.close()          # Windows：不关会锁住临时目录
        finally:
            os.chdir(cwd)


def test_postgres_dsn_failure_is_runtime_error_without_leaking_password():
    """`postgresql://` 分支：不管是「连不上」还是「驱动没装」，都必须打码、不许漏密码。"""
    dsn = "postgresql://kfpa:supersecret@127.0.0.1:1/kfpa?connect_timeout=1"
    with pytest.raises(RuntimeError) as excinfo:
        st.make_store(dsn)
    message = str(excinfo.value)
    assert "supersecret" not in message
    assert "***" in message or "未安装" in message or "缺少" in message


def test_mask_dsn_never_keeps_the_password():
    assert st.mask_dsn("postgresql://u:p@h:55440/db") == "postgresql://u:***@h:55440/db"
    assert st.mask_dsn("postgresql://u@h/db") == "postgresql://u@h/db"      # 无密码
    assert st.mask_dsn("postgresql://h/db") == "postgresql://h/db"          # 无凭据
    assert st.mask_dsn("/tmp/plain.db") == "/tmp/plain.db"                  # 非 DSN
    assert "hunter2" not in st.mask_dsn("postgres://u:hunter2@h/db?schema=public")


def test_postgres_detection_and_migrations_dir():
    assert st.is_postgres_dsn("postgresql://x") and st.is_postgres_dsn("postgres://x")
    assert not st.is_postgres_dsn("sqlite://x") and not st.is_postgres_dsn("kfpa.db")
    migrations = st.migrations_dir()
    assert (migrations / "0001_init.sql").is_file()
    assert (migrations / "0002_reviews.sql").is_file()


def test_table_constants_and_protocol():
    store = st.Store(":memory:")
    assert isinstance(store, st.StoreProtocol)
    # P3（D5-3.1）升到 "4"；P2 常量 "3" 保留为历史口径。
    assert st.SCHEMA_VERSION == st.P3_SCHEMA_VERSION == "4"
    assert st.P0_SCHEMA_VERSION == "1" and st.P1_SCHEMA_VERSION == "2"
    assert st.P2_SCHEMA_VERSION == "3"
    assert len(st.BUSINESS_TABLES) == 6 and len(st.TABLE_NAMES) == 7
    assert st.P1_TABLE_NAMES == ("ledger", "knowledge") and st.P2_TABLE_NAMES == ("reviews",)
    assert st.P3_TABLE_NAMES == ("fastlane_requests", "knowledge_samples")
    assert set(st.ALL_TABLE_NAMES) == set(st.TABLE_NAMES) | {
        "ledger", "knowledge", "reviews", "fastlane_requests", "knowledge_samples"}
    assert st.META_TABLE == "meta" and st.DB_URL_ENV == "KFPA_DB_URL"
    store.close()


# ---------------------------------------------------------------- store/sqlite.py


def test_meta_and_round_upsert_semantics(tmp_path):
    store = st.Store(str(tmp_path / "s.db"))
    assert set(st.ALL_TABLE_NAMES) <= set(store.list_tables())
    assert store.get_meta("schema_version") == st.SCHEMA_VERSION
    store.set_meta("note", "x")
    assert store.get_meta("note") == "x"
    store.set_meta("note", "y")                             # ON CONFLICT 覆盖
    assert store.get_meta("note") == "y"
    assert store.get_meta("absent") is None

    assert store.get_round(1) is None
    created = store.upsert_round(Round(round_no=1, design_ref="d.md"))
    assert created.state is RoundState.DESIGN_IN
    store.set_round_state(1, RoundState.RELEASED)
    store.upsert_round(Round(round_no=1, design_ref="d2.md"))    # 已有 → 只更新 design_ref
    assert store.get_round(1).state is RoundState.RELEASED
    assert store.get_round(1).design_ref == "d2.md"
    store.set_round_state(7, RoundState.BLOCKED)                 # 自动补骨架
    assert store.get_round(7).state is RoundState.BLOCKED
    store.close()


def test_ticket_filters_and_close_idempotence(tmp_path):
    store = st.Store(str(tmp_path / "t.db"))
    store.add_ticket(QATicket("R1-001", 1, "§4", "a", "b", "A", impact=TicketImpact.HIGH))
    store.add_ticket(QATicket("R1-002", 1, "§5", "a", "b", "A", impact=TicketImpact.MEDIUM))
    store.add_ticket(QATicket("R1-003", 1, "§6", "a", "b", "A", impact=TicketImpact.LOW))
    assert store.get_ticket("nope") is None
    assert [t.ticket_id for t in store.list_open_high_tickets(1)] == ["R1-001"]
    assert [t.ticket_id for t in store.list_tickets()] == ["R1-001", "R1-002", "R1-003"]
    assert [t.ticket_id for t in store.list_tickets(1, TicketStatus.OPEN)] == \
        ["R1-001", "R1-002", "R1-003"]
    assert [t.ticket_id for t in store.list_tickets(None, None, TicketImpact.HIGH)] == ["R1-001"]
    assert [t.ticket_id for t in store.list_tickets(1, None, "low")] == ["R1-003"]

    store.close_ticket("R1-001", "选 A")
    assert store.get_ticket("R1-001").status is TicketStatus.CLOSED
    assert store.list_open_high_tickets(1) == []
    store.add_ticket(QATicket("R1-001", 1, "§4 改", "a2", "b2", "B", impact=TicketImpact.HIGH))
    again = store.get_ticket("R1-001")
    assert again.status is TicketStatus.CLOSED and again.answer == "选 A"    # 关闭态不被覆盖
    assert again.location == "§4 改"                                         # 票面可更新
    with pytest.raises(KeyError):
        store.close_ticket("missing", "x")
    store.close()


def test_package_gate_failure_snapshot_roundtrip(tmp_path):
    store = st.Store(str(tmp_path / "p.db"))
    assert store.get_package(1) is None
    minimal = TaskPackage(round_no=1, feature="", goal="")
    assert store.save_package(minimal)                       # 走 payload 缺省分支
    assert store.get_package(1).round_no == 1
    full = TaskPackage(round_no=1, feature="f", goal="g",
                       acceptance=[AcceptanceCriterion("AC1", "t")], boundaries=["b"],
                       interface_contract=["i"], prohibitions=["p"], feedback=["AVOID[x]"],
                       closed_tickets=["R1-001"])
    assert store.save_package(full) == store.save_package(full)   # 同轮覆盖、hash 稳定

    assert store.list_gate_reports(1) == []
    store.save_gate_report(GateReport(1, GateState.NEEDS_FIX, reasons=["r"],
                                      failing_criteria=["AC1"], attempt=1, reason_key="k1"))
    store.save_gate_report(GateReport(1, GateState.PASS, attempt=2))
    reports = store.list_gate_reports(1)
    assert [r.state for r in reports] == [GateState.NEEDS_FIX, GateState.PASS]
    assert reports[0].reason_key == "k1" and reports[0].escalated is False

    store.record_failure(FailureRecord(1, 1, "boom", "AVOID[k1] x", reason_key="k1"))
    store.record_failure(FailureRecord(2, 1, "other", ""))       # 空 avoid_pattern 被过滤
    assert store.count_same_failure(1, "k1") == 1
    assert store.count_same_failure(1, "boom") == 1              # reason_key 空 → 退化按 reason
    assert store.list_avoid_patterns(1) == ["AVOID[k1] x"]
    assert store.list_avoid_patterns() == ["AVOID[k1] x"]

    assert store.get_snapshot(9) is None
    store.save_snapshot(Snapshot(9, "h1", GateState.PASS, git_sha="a", tag="kfpa-r9"))
    store.save_snapshot(Snapshot(9, "h2", GateState.BLOCKED))    # 同轮覆盖
    snaps = store.list_snapshots()
    assert len(snaps) == 1 and snaps[0].package_hash == "h2" and snaps[0].tag == ""
    assert store.get_snapshot(9).gate_state is GateState.BLOCKED
    store.close()


def test_ledger_candidate_filters_and_keyerror(tmp_path):
    store = st.Store(str(tmp_path / "k.db"))
    store.add_ledger_entry(LedgerEntry(round_no=1, role="ARCHITECT", model_ref="m",
                                       tokens_in=3, tokens_out=5))
    store.add_ledger_entry(LedgerEntry(round_no=2, role="FLASH", model_ref="m",
                                       tokens_in=1, tokens_out=1, cost=0.5))
    assert [e.role for e in store.list_ledger(1)] == ["ARCHITECT"]
    assert len(store.list_ledger()) == 2

    assert store.get_candidate("K1") is None
    store.add_candidate(KnowledgeCandidate(cid="K1", round_no=1, kind="success", content="c",
                                           confidence=0.9))
    store.add_candidate(KnowledgeCandidate(cid="K1", round_no=1, kind="avoid", content="c2",
                                           confidence=0.1, status=CandidateStatus.DROPPED))
    assert store.get_candidate("K1").kind == "avoid"                     # 同 cid 覆盖
    assert [c.cid for c in store.list_candidates("dropped")] == ["K1"]   # 字符串筛选
    assert [c.cid for c in store.list_candidates(CandidateStatus.DROPPED)] == ["K1"]  # 枚举筛选
    assert [c.cid for c in store.list_candidates()] == ["K1"]            # 不筛
    promoted = store.promote_candidate("K1", 3)
    assert promoted.status is CandidateStatus.PROMOTED and promoted.reused_by == [3]
    assert store.promote_candidate("K1", 3).reused_by == [3]             # 重复轮次不重复记
    store.drop_candidate("K1", "低置信")
    assert store.get_candidate("K1").status is CandidateStatus.DROPPED
    for bad in (lambda: store.promote_candidate("absent", 1),
                lambda: store.drop_candidate("absent", "r")):
        with pytest.raises(KeyError):
            bad()
    store.close()


def test_review_reports_and_tolerant_parsing_of_corrupt_rows(tmp_path):
    """#48 的「宽容解析」：库里 JSON 坏了 / severity 是未知值，都不许把读侧打崩。"""
    store = st.Store(str(tmp_path / "r.db"))
    assert store.list_review_reports(1) == []
    store.add_review_report(ReviewReport(round_no=1, provider="mock_hetero", model_ref="m",
                                         context_id="rev-1-1-a", parse_ok=True, verdict="fail",
                                         findings=[ReviewFinding(Severity.MAJOR, "loc", "i", "s",
                                                                 "AC1")]))
    store.add_review_report(ReviewReport(round_no=1, provider="mock", model_ref="",
                                         context_id="rev-1-2-b", parse_ok=False,
                                         verdict="unknown", attempt=2))
    assert [r.context_id for r in store.list_review_reports(1)] == ["rev-1-1-a", "rev-1-2-b"]
    assert len(store.list_review_reports()) == 2 and store.list_review_reports(9) == []
    first = store.list_review_reports(1)[0]
    assert first.parse_ok is True and [f.criterion for f in first.blockers()] == ["AC1"]

    store._conn.execute("UPDATE reviews SET findings = ? WHERE context_id = ?",
                        ("{not json", "rev-1-1-a"))
    store._conn.execute("UPDATE reviews SET findings = ? WHERE context_id = ?",
                        (json.dumps([{"severity": "weird"}]), "rev-1-2-b"))
    store._conn.execute("UPDATE knowledge SET reused_by = ? WHERE cid = ?", ("not-json", "K1"))
    store._conn.commit()
    repaired = {r.context_id: r for r in store.list_review_reports(1)}
    assert repaired["rev-1-1-a"].findings == []                          # 坏 JSON → 空列表
    assert repaired["rev-1-2-b"].findings[0].severity is Severity.INFO   # 未知 severity → INFO
    store.close()
    store.close()                                                        # 重复 close 安全


def test_closed_store_raises_sqlite_error(tmp_path):
    store = st.Store(":memory:")
    store.save_package(TaskPackage(round_no=5, feature="f", goal="g"))
    package = store.get_package(5)
    assert package.acceptance == [] and package.feedback == [] and package.closed_tickets == []
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.list_snapshots()          # 已关闭 → 数据库错误（CLI 边界归 5）


# ---------------------------------------------------------------- R-31 rc 三分


def test_exception_exit_code_three_way_r31():
    """A18 的单测级口径：未知值 2 由 argparse 管；这里钉「依赖凭据 4 / I-O 与 DB 5」。"""
    assert cli._exception_exit_code(ImportError("no module")) == 4
    assert cli._exception_exit_code(
        RuntimeError("模型调用失败（角色 REVIEWER，provider=litellm）：无 key")) == 4
    assert cli._exception_exit_code(RuntimeError("未安装 litellm")) == 4
    assert cli._exception_exit_code(
        pipeline.DependencyMissingError("缺 langgraph-checkpoint-sqlite")) == 4
    assert cli._exception_exit_code(RuntimeError("缺少 pyyaml")) == 4
    assert cli._exception_exit_code(OSError("disk")) == 5
    assert cli._exception_exit_code(sqlite3.OperationalError("no such table")) == 5
    assert cli._exception_exit_code(ValueError("其它")) == 5
    assert (cli.EXIT_USAGE, cli.EXIT_DEP_MISSING, cli.EXIT_IO) == (2, 4, 5)

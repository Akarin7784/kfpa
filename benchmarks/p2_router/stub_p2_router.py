"""P2 `p2/router` 的实现方桩（R-30：实现方桩落 `benchmarks/**`，由镜鉴搬进 `tests/**`）。

为什么需要它：A20 的覆盖率门槛用 `pytest --cov=kfpa` 采集，而台架与多数测试是用**子进程**
跑 `python -m kfpa ...`，进程内覆盖收不到——实测 `src/kfpa/cli.py` 在 `pytest --cov` 下是 **0%**（327 语句）。
本桩把「CLI / config 路由 / mock_hetero / store 新表」改成**进程内调用**，覆盖才有数。

跑法（干净环境）::

    $env:PYTHONPATH="src"
    python -m pytest benchmarks/p2_router/stub_p2_router.py -q

镜像覆盖点：`cli.main` 的 init/round/gate/history/rollback 分支、`_open_store` 的
DSN/环境变量/路径优先级、`config.resolve_provider` 三级、`llm.MockHeteroLLM` 的
defect/clean/malformed/角色不符、`build_llm` 的路由与强制、`store` 的 reviews 表与常量。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import kfpa.cli as cli  # noqa: E402
import kfpa.store as st  # noqa: E402
from kfpa.config import PROVIDERS, normalize_provider, resolve_provider  # noqa: E402
from kfpa.llm import (HETERO_INJECTIONS, MockHeteroLLM, MockScriptError, RoleRoutingLLM,  # noqa: E402
                      build_llm)
from kfpa.schemas import (CandidateStatus, KnowledgeCandidate, LedgerEntry,  # noqa: E402
                          ReviewFinding, ReviewReport, Round, RoundState, Severity)

DESIGN = """# 桩用设计
FEATURE: stub
GOAL: 覆盖 router 相关分支
AC: 接口契约含错误码
IF: POST /x 出参 {ok, code}
NO: 不新增依赖
[DEFECT:missing_error_code] AC1: 接口契约只写了成功返回
"""

CLEAN = """FEATURE: stub-clean
GOAL: 无缺陷样本
AC: 正常断言
"""


@pytest.fixture()
def roles() -> dict:
    return {
        "ARCHITECT": {"model": "x/arch"},
        "REVIEWER": {"model": "x/review", "provider": "mock_hetero"},
        "FLASH": {"model": "x/flash"},
        "EMBEDDING": {"model": "x/embed"},
    }


# ---------------------------------------------------------------- config 路由


def test_provider_whitelist_and_priority(roles):
    assert PROVIDERS == ("mock", "mock_hetero", "litellm")
    assert resolve_provider(roles, "REVIEWER") == "mock_hetero"
    assert resolve_provider(roles, "ARCHITECT") == "mock"          # 角色没写 → 默认
    assert resolve_provider(roles, "ARCHITECT", "litellm") == "litellm"  # CLI 覆盖优先
    assert normalize_provider(None) == "mock"
    with pytest.raises(ValueError, match="未知 provider"):
        resolve_provider(roles, "ARCHITECT", "no-such-provider")


# ---------------------------------------------------------------- mock_hetero


def test_mock_hetero_defect_clean_and_injections():
    defect = json.loads(MockHeteroLLM().complete("REVIEWER", "", DESIGN))
    assert defect["verdict"] == "fail"
    assert defect["findings"][0]["severity"] == "major"
    assert defect["findings"][0]["criterion"] == "AC1"

    clean = json.loads(MockHeteroLLM().complete("REVIEWER", "", CLEAN))
    assert clean == {"findings": [], "verdict": "pass"}            # ①无缺陷 → pass + []

    major = json.loads(MockHeteroLLM(inject="major_with_criterion").complete("REVIEWER", "", CLEAN))
    assert major["findings"][0]["criterion"] == "AC1"
    no_criterion = json.loads(MockHeteroLLM(inject="major_without_criterion").complete("REVIEWER", "", CLEAN))
    assert no_criterion["findings"][0]["criterion"] == ""
    assert MockHeteroLLM(inject="malformed").complete("REVIEWER", "", CLEAN).startswith("（mock_hetero）")
    with pytest.raises(MockScriptError):
        MockHeteroLLM(inject="nope")
    with pytest.raises(MockScriptError):
        MockHeteroLLM().complete("ARCHITECT", "", CLEAN)           # 异构位只做评审
    assert set(HETERO_INJECTIONS) >= {"none", "major_with_criterion", "malformed"}


def test_build_llm_routes_by_role(roles):
    llm = build_llm(roles)
    assert isinstance(llm, RoleRoutingLLM)
    assert [llm.provider_for(r) for r in ("ARCHITECT", "REVIEWER", "FLASH")] == ["mock", "mock_hetero", "mock"]
    assert llm.adapter_for("REVIEWER").name == "mock_hetero"
    assert llm.adapter_for("ARCHITECT") is llm.adapter_for("ARCHITECT")   # 缓存
    forced = build_llm(roles, provider="mock")
    assert forced.name == "mock"                                        # 强制覆盖
    with pytest.raises(ValueError, match="未知 provider"):
        build_llm(roles, provider="nope")


# ---------------------------------------------------------------- store：reviews / P2 常量


def test_store_reviews_and_p2_constants():
    store = st.Store(":memory:")
    # P3（D5-3.1）升到 "4"；P2 常量 "3" 保留为历史口径。
    assert store.get_meta("schema_version") == st.SCHEMA_VERSION == st.P3_SCHEMA_VERSION == "4"
    assert st.P2_SCHEMA_VERSION == "3"
    assert set(st.ALL_TABLE_NAMES) <= set(store.list_tables())
    assert "reviews" in store.list_tables()
    assert st.P2_TABLE_NAMES == ("reviews",)
    assert st.P3_TABLE_NAMES == ("fastlane_requests", "knowledge_samples")

    report = ReviewReport(
        round_no=1, provider="mock_hetero", model_ref="x/review", context_id="rev-1-1-abc",
        parse_ok=True, verdict="fail",
        findings=[ReviewFinding(Severity.MAJOR, "任务包", "issue", "sug", "AC1")],
    )
    store.add_review_report(report)
    got = store.list_review_reports(1)
    assert len(got) == 1 and got[0].context_id == "rev-1-1-abc"
    assert [f.criterion for f in got[0].blockers()] == ["AC1"]
    assert store.list_review_reports(2) == []
    assert store.list_review_reports() and store.list_review_reports()[0].parse_ok is True

    # 顺带把 P1 的两条新路径也走到（账本 / 沉淀）
    store.add_ledger_entry(LedgerEntry(round_no=1, role="REVIEWER", model_ref="x/review", tokens_in=1, tokens_out=2))
    assert store.list_ledger(1)[0].total_tokens() == 3
    store.add_candidate(KnowledgeCandidate(cid="K1", round_no=1, kind="success", content="c", confidence=0.9))
    assert store.get_candidate("K1").status is CandidateStatus.QUARANTINE


def test_store_protocol_satisfied():
    assert isinstance(st.Store(":memory:"), st.StoreProtocol)


# ---------------------------------------------------------------- cli：进程内覆盖


def test_cli_basics_and_error_codes():
    assert cli.main(["--help"]) == 0
    assert cli.main([]) == 0
    assert cli.main(["nope"]) == 2
    assert cli.build_parser().prog == "kfpa"


def test_cli_init_gate_history_rollback():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "t.db")
        assert cli.main(["init", "--db", db]) == 0
        assert cli.main(["history", "--db", db]) == 0
        assert cli.main(["gate", "1", "--db", db]) == 3          # 无门禁记录 → 3（R-1）
        assert cli.main(["rollback", "1", "--db", db]) == 5     # 无快照 → 5
        assert cli.main(["gate", "1", "--db", str(Path(td) / "no" / "x.db")]) == 5  # 库打不开 → 5


def test_cli_round_with_stub_pipeline(tmp_path):
    """用桩 pipeline/executor 覆盖 `_open_store`、`_round_exit_code`、injection/checkpoint 分支。"""
    captured: dict = {}

    def fake_run_round(round_no, design_text, design_ref, *, store, llm, executor, workdir,
                       max_attempts=3, dry_run=True, injection="none", checkpointer=None,
                       roles=None, review_provider=None, review_model_ref=None, **kwargs):
        captured.update(
            round_no=round_no, injection=injection, checkpointer=checkpointer, roles=roles,
            review_provider=review_provider, review_model_ref=review_model_ref,
            workdir=str(workdir), llm=llm,
        )
        from kfpa.schemas import GateReport, GateState, RoundResult
        return RoundResult(
            round_no=round_no, state=RoundState.SNAPSHOTTED,
            gate=GateReport(round_no=round_no, state=GateState.PASS), message="桩",
        )

    fake_pipeline = types.ModuleType("kfpa.pipeline")
    fake_pipeline.run_round = fake_run_round
    fake_pipeline.make_checkpointer = lambda path=None: ("saver", str(path))
    fake_executor = types.ModuleType("kfpa.executor")

    class _Mock:
        name = "mock"

    fake_executor.MockExecutor = _Mock
    fake_executor.AiderExecutor = _Mock

    design = tmp_path / "d.md"
    design.write_text(DESIGN, encoding="utf-8")
    db = str(tmp_path / "t.db")
    old_pipeline, old_executor = sys.modules.get("kfpa.pipeline"), sys.modules.get("kfpa.executor")
    sys.modules["kfpa.pipeline"] = fake_pipeline
    sys.modules["kfpa.executor"] = fake_executor
    try:
        rc = cli.main(["round", "--design", str(design), "--round", "1", "--db", db,
                       "--workdir", str(tmp_path / "w"), "--json"])
        assert rc == 0
        assert captured["injection"] == "none"
        assert captured["review_provider"] == "mock_hetero"      # 按角色路由的 REVIEWER
        assert captured["review_model_ref"] == "deepseek/deepseek-v4.1-flash"  # 从 config 取
        assert captured["checkpointer"][1].replace("\\", "/").endswith("w/checkpoints.sqlite")
        assert captured["roles"].get("REVIEWER")

        rc = cli.main(["round", "--design", str(design), "--round", "2", "--db", db,
                       "--workdir", str(tmp_path / "w"), "--inject", "test_fail",
                       "--checkpoint-db", str(tmp_path / "cp.sqlite")])
        assert rc == 0 and captured["injection"] == "test_fail"
        assert captured["checkpointer"][1].endswith("cp.sqlite")

        assert cli.main(["round", "--design", str(tmp_path / "nope.md"), "--round", "3", "--db", db]) == 4
    finally:
        for name, old in (("kfpa.pipeline", old_pipeline), ("kfpa.executor", old_executor)):
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def test_cli_open_store_priority_r25(tmp_path, monkeypatch):
    """R-25.1：显式 `--db`（DSN 或普通路径）> `KFPA_DB_URL` > 默认 `kfpa.db`。"""
    monkeypatch.setenv("KFPA_DB_URL", f"sqlite://{tmp_path / 'env.db'}")
    assert cli._open_store(str(tmp_path / "plain.db")).db_path.endswith("plain.db")   # 显式路径赢
    assert cli._open_store(f"sqlite://{tmp_path / 'dsn.db'}").db_path.endswith("dsn.db")  # 显式 DSN 赢
    assert cli._open_store("kfpa.db").db_path.endswith("env.db")                      # 默认值 → env 生效
    monkeypatch.delenv("KFPA_DB_URL", raising=False)
    assert cli._open_store(str(tmp_path / "plain2.db")).db_path.endswith("plain2.db")


def test_cli_default_workdir():
    assert cli.default_workdir(3).replace("\\", "/").endswith("runs/round-3")


def test_cli_register_plugins_mounts_eval():
    parser = cli.build_parser()
    sub = [a for a in parser._actions if getattr(a, "choices", None) and "round" in a.choices]
    assert sub, "找不到子命令 action"
    names = set(sub[0].choices)
    assert {"init", "round", "gate", "history", "rollback"} <= names

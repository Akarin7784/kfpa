"""R-30 交接改写：provider 路由 / mock_hetero / store P2 表 / CLI 进程内覆盖（镜鉴独立复核版）。

来源：磐石 `benchmarks/p2_router/stub_p2_router.py`（11 项实现方桩）。
**改写口径（R-30）**：

1. 不复用他的分组与断言写法：按「口径 → 反例」重组，期望值从 D4 §3.2 / R-24 / R-25.1 出发写；
2. **补一条端到端反例**：拿仓库真实的 `benchmarks/review/seed.jsonl` 里那条注入缺陷样本，
   走 `review.run_review` → `apply_review_to_gate`，验 G8 真的按 marker 里的 criterion 阻塞
   —— 只测「对一段手写文本返回 fail」证明不了种子集能被评审位识别；
3. `mock_hetero` 的 clean 分支单列一条（R-24 ①：无缺陷必须 `pass` + `findings=[]`，
   否则 REVIEWER 已默认进主循环，会把 P0/P1 happy path 全判红）。

为什么必须进程内：`pytest --cov` 收不到 CLI 子进程的行，`cli.py` 在台架驱动下覆盖为 0%。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

import kfpa.cli as cli
import kfpa.eval as eval_mod
import kfpa.review as review_mod
import kfpa.store as st
from kfpa import gates
from kfpa.config import PROVIDERS, normalize_provider, resolve_provider
from kfpa.llm import (HETERO_INJECTIONS, MockHeteroLLM, MockScriptError, RoleRoutingLLM,
                      build_llm)
from kfpa.schemas import (CandidateStatus, GateState, KnowledgeCandidate, LedgerEntry,
                          ReviewFinding, ReviewReport, Round, RoundState, Severity, TaskPackage)

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "benchmarks" / "review" / "seed.jsonl"

#: marker 口径（R-24 ②）：`[DEFECT:<kind>] <criterion>: <描述>` 藏在六段正文里
DEFECT_TEXT = """FEATURE: stub
GOAL: 覆盖 router 分支
AC: 接口契约含错误码
IF: POST /x 出参 {ok, code}
[DEFECT:missing_error_code] AC2: 接口契约只写了成功返回
"""
CLEAN_TEXT = "FEATURE: stub-clean\nGOAL: 无缺陷样本\nAC: 正常断言\n"


@pytest.fixture()
def roles() -> dict:
    return {
        "ARCHITECT": {"model": "x/arch"},
        "REVIEWER": {"model": "x/review", "provider": "mock_hetero"},
        "FLASH": {"model": "x/flash"},
        "EMBEDDING": {"model": "x/embed"},
    }


def _first_defect_sample() -> dict:
    for line in SEED.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sample = json.loads(line)
        if sample.get("injected_defects"):
            return sample
    pytest.fail("种子集里没有注入缺陷样本")


# ---------------------------------------------------------------- config：provider 路由


def test_provider_whitelist_is_frozen_by_d4(roles):
    assert PROVIDERS == ("mock", "mock_hetero", "litellm")


def test_role_provider_resolution_and_default(roles):
    assert resolve_provider(roles, "REVIEWER") == "mock_hetero"   # 角色显式配了异构
    assert resolve_provider(roles, "ARCHITECT") == "mock"         # 未配 → 默认 mock
    assert resolve_provider(roles, "FLASH") == "mock"


def test_cli_provider_override_beats_role_config(roles):
    assert resolve_provider(roles, "REVIEWER", "litellm") == "litellm"
    assert resolve_provider(roles, "ARCHITECT", "mock_hetero") == "mock_hetero"


def test_unknown_provider_value_is_rejected(roles):
    assert normalize_provider(None) == "mock"
    with pytest.raises(ValueError, match="未知 provider"):
        resolve_provider(roles, "ARCHITECT", "no-such-provider")


# ---------------------------------------------------------------- mock_hetero 剧本


def test_hetero_clean_sample_is_pass_with_empty_findings():
    """R-24 ①：无缺陷 → `verdict=pass` + `findings=[]`（不许默认给 major 把 happy path 判红）。"""
    out = json.loads(MockHeteroLLM().complete("REVIEWER", "", CLEAN_TEXT))
    assert out == {"findings": [], "verdict": "pass"}


def test_hetero_reads_defect_marker_criterion():
    out = json.loads(MockHeteroLLM().complete("REVIEWER", "", DEFECT_TEXT))
    assert out["verdict"] == "fail"
    assert [f["criterion"] for f in out["findings"]] == ["AC2"]
    assert out["findings"][0]["severity"] == "major"


def test_hetero_seed_defect_reaches_g8_blocking_through_real_path():
    """端到端：真实种子集样本 → run_review → apply_review_to_gate → G8 阻塞。"""
    sample = _first_defect_sample()
    criterion = sample["injected_defects"][0]["criterion"]
    # 用仓库自己的还原器，别手搓 TaskPackage（种子集的 acceptance 是 dict 列表）
    package = eval_mod.sample_to_package(sample)
    report = review_mod.run_review(package, MockHeteroLLM(), provider="mock_hetero",
                                   round_no=1, attempt=1, model_ref="x/review")
    assert report.parse_ok is True
    assert report.verdict == "fail"
    assert criterion in [f.criterion for f in report.blockers()]

    gate = gates.evaluate(1, package, _ok_exec(package), _NullStore(), attempt=1)
    merged = review_mod.apply_review_to_gate(report, gate)
    assert merged.state is GateState.NEEDS_FIX
    assert criterion in merged.failing_criteria


def test_hetero_injections_cover_every_g8_branch():
    clean = CLEAN_TEXT
    major = json.loads(MockHeteroLLM(inject="major_with_criterion").complete("REVIEWER", "", clean))
    assert major["findings"][0]["criterion"] == "AC1"
    no_crit = json.loads(MockHeteroLLM(inject="major_without_criterion").complete(
        "REVIEWER", "", clean))
    assert no_crit["findings"][0]["criterion"] == ""      # 无 criterion → G8 不阻塞
    assert MockHeteroLLM(inject="malformed").complete("REVIEWER", "", clean).startswith("（mock_hetero）")
    assert set(HETERO_INJECTIONS) >= {"none", "major_with_criterion",
                                      "major_without_criterion", "malformed"}


def test_hetero_rejects_unknown_injection_and_foreign_role():
    with pytest.raises(MockScriptError):
        MockHeteroLLM(inject="nope")
    with pytest.raises(MockScriptError):
        MockHeteroLLM().complete("ARCHITECT", "", CLEAN_TEXT)   # 异构位只做评审


def test_build_llm_routes_each_role_and_caches_adapter(roles):
    llm = build_llm(roles)
    assert isinstance(llm, RoleRoutingLLM)
    assert [llm.provider_for(r) for r in ("ARCHITECT", "REVIEWER", "FLASH")] == \
        ["mock", "mock_hetero", "mock"]
    assert llm.adapter_for("REVIEWER").name == "mock_hetero"
    assert llm.adapter_for("ARCHITECT") is llm.adapter_for("ARCHITECT")


def test_build_llm_forced_provider_and_unknown_rejected(roles):
    assert build_llm(roles, provider="mock").name == "mock"
    with pytest.raises(ValueError, match="未知 provider"):
        build_llm(roles, provider="nope")


# ---------------------------------------------------------------- store：P2 表与契约


def test_store_schema_version_is_p2_and_reviews_table_exists():
    store = st.Store(":memory:")
    # P3（D5-3.1）把 schema 升到 4：P2 常量作为历史仍是 "3"，当前版本跟 P3 走（R-20/R-21 同批同步）。
    assert st.SCHEMA_VERSION == st.P3_SCHEMA_VERSION == "4"
    assert st.P2_SCHEMA_VERSION == "3"
    assert set(st.ALL_TABLE_NAMES) <= set(store.list_tables())
    assert st.P2_TABLE_NAMES == ("reviews",)
    assert st.P3_TABLE_NAMES == ("fastlane_requests", "knowledge_samples")
    assert "reviews" in store.list_tables()


def test_store_review_report_roundtrip_and_blocker_rule():
    store = st.Store(":memory:")
    report = ReviewReport(
        round_no=1, provider="mock_hetero", model_ref="x/review", context_id="rev-1-1-abc",
        parse_ok=True, verdict="fail",
        findings=[ReviewFinding(Severity.MAJOR, "任务包", "issue", "sug", "AC1")],
    )
    store.add_review_report(report)
    got = store.list_review_reports(1)
    assert len(got) == 1 and got[0].context_id == "rev-1-1-abc"
    assert [f.criterion for f in got[0].blockers()] == ["AC1"]
    assert store.list_review_reports(2) == []          # 按轮次过滤
    assert store.list_review_reports()[0].parse_ok is True   # 不带轮次 = 全量


def test_store_satisfies_protocol_and_p1_paths():
    store = st.Store(":memory:")
    assert isinstance(store, st.StoreProtocol)
    store.add_ledger_entry(LedgerEntry(round_no=1, role="REVIEWER", model_ref="x/review",
                                       tokens_in=1, tokens_out=2))
    assert store.list_ledger(1)[0].total_tokens() == 3
    store.add_candidate(KnowledgeCandidate(cid="K1", round_no=1, kind="success", content="c",
                                           confidence=0.9))
    assert store.get_candidate("K1").status is CandidateStatus.QUARANTINE


# ---------------------------------------------------------------- CLI：进程内覆盖


def test_cli_help_and_usage_exit_codes():
    assert cli.main(["--help"]) == 0
    assert cli.main([]) == 0            # 无子命令 → 打印帮助，不是错误
    assert cli.main(["nope"]) == 2      # 未知子命令 = 用法错误
    assert cli.build_parser().prog == "kfpa"


def test_cli_mounts_p0_p1_p2_subcommands():
    parser = cli.build_parser()
    subs = [a for a in parser._actions if getattr(a, "choices", None) and "round" in a.choices]
    assert subs, "找不到子命令 action"
    names = set(subs[0].choices)
    assert {"init", "round", "gate", "history", "rollback"} <= names
    assert "eval" in names or "ledger" in names, "P1/P2 插件一个都没挂上"


def test_cli_init_history_gate_rollback(tmp_path):
    db = str(tmp_path / "t.db")
    assert cli.main(["init", "--db", db]) == 0
    assert cli.main(["history", "--db", db]) == 0
    assert cli.main(["gate", "1", "--db", db]) == 3            # 无门禁记录 → 3（R-1）
    assert cli.main(["rollback", "1", "--db", db]) == 5        # 无快照 → 5
    assert cli.main(["gate", "1", "--db", str(tmp_path / "no" / "x.db")]) == 5


def test_cli_default_workdir_is_round_scoped():
    assert cli.default_workdir(3).replace("\\", "/").endswith("runs/round-3")


def test_cli_open_store_priority_explicit_beats_env(tmp_path, monkeypatch):
    """R-25.1：显式 `--db`（DSN 或普通路径）> `KFPA_DB_URL` > 默认 `kfpa.db`。"""
    monkeypatch.setenv("KFPA_DB_URL", f"sqlite://{tmp_path / 'env.db'}")
    assert cli._open_store(str(tmp_path / "plain.db")).db_path.endswith("plain.db")
    assert cli._open_store(f"sqlite://{tmp_path / 'dsn.db'}").db_path.endswith("dsn.db")
    assert cli._open_store("kfpa.db").db_path.endswith("env.db")   # 字面量默认 → env 生效
    monkeypatch.delenv("KFPA_DB_URL", raising=False)
    assert cli._open_store(str(tmp_path / "plain2.db")).db_path.endswith("plain2.db")


def _patch_pipeline(monkeypatch, captured: dict, fake_run_round, make_checkpointer) -> None:
    fake_pipeline = types.ModuleType("kfpa.pipeline")
    fake_pipeline.run_round = fake_run_round
    fake_pipeline.make_checkpointer = make_checkpointer
    fake_executor = types.ModuleType("kfpa.executor")

    class _Mock:
        name = "mock"

    fake_executor.MockExecutor = _Mock
    fake_executor.AiderExecutor = _Mock
    monkeypatch.setitem(sys.modules, "kfpa.pipeline", fake_pipeline)
    monkeypatch.setitem(sys.modules, "kfpa.executor", fake_executor)


def test_cli_round_passes_role_routed_provider_and_default_checkpoint(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run_round(round_no, design_text, design_ref, *, store, llm, executor, workdir,
                       max_attempts=3, dry_run=True, injection="none", checkpointer=None,
                       roles=None, review_provider=None, review_model_ref=None, **kwargs):
        captured.update(round_no=round_no, injection=injection, checkpointer=checkpointer,
                        roles=roles, review_provider=review_provider,
                        review_model_ref=review_model_ref, workdir=str(workdir), llm=llm)
        from kfpa.schemas import GateReport, RoundResult
        return RoundResult(round_no=round_no, state=RoundState.SNAPSHOTTED,
                           gate=GateReport(round_no=round_no, state=GateState.PASS), message="桩")

    _patch_pipeline(monkeypatch, captured, fake_run_round,
                    lambda path=None: ("saver", str(path)))
    design = tmp_path / "d.md"
    design.write_text(DEFECT_TEXT, encoding="utf-8")
    db = str(tmp_path / "t.db")

    assert cli.main(["round", "--design", str(design), "--round", "1", "--db", db,
                     "--workdir", str(tmp_path / "w"), "--json"]) == 0
    assert captured["injection"] == "none"
    assert captured["review_provider"] == "mock_hetero"          # 按角色路由的 REVIEWER
    assert captured["review_model_ref"]                          # 从 config 取，非空
    assert captured["checkpointer"][1].replace("\\", "/").endswith("w/checkpoints.sqlite")
    assert captured["roles"].get("REVIEWER")


def test_cli_round_injection_and_explicit_checkpoint(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run_round(round_no, design_text, design_ref, *, store, llm, executor, workdir,
                       max_attempts=3, dry_run=True, injection="none", checkpointer=None,
                       roles=None, review_provider=None, review_model_ref=None, **kwargs):
        captured.update(injection=injection, checkpointer=checkpointer)
        from kfpa.schemas import GateReport, RoundResult
        return RoundResult(round_no=round_no, state=RoundState.SNAPSHOTTED,
                           gate=GateReport(round_no=round_no, state=GateState.PASS), message="桩")

    _patch_pipeline(monkeypatch, captured, fake_run_round,
                    lambda path=None: ("saver", str(path)))
    design = tmp_path / "d.md"
    design.write_text(DEFECT_TEXT, encoding="utf-8")
    cp = tmp_path / "cp.sqlite"
    assert cli.main(["round", "--design", str(design), "--round", "2",
                     "--db", str(tmp_path / "t.db"), "--workdir", str(tmp_path / "w"),
                     "--inject", "test_fail", "--checkpoint-db", str(cp)]) == 0
    assert captured["injection"] == "test_fail"
    assert captured["checkpointer"][1].endswith("cp.sqlite")


def test_cli_round_missing_design_returns_4(tmp_path):
    assert cli.main(["round", "--design", str(tmp_path / "nope.md"), "--round", "3",
                     "--db", str(tmp_path / "t.db")]) == 4


# ---------------------------------------------------------------- 桩


class _NullStore:
    """只满足 `gates.evaluate` 的同因计数查询（G5 用不到）。"""

    def count_same_failure(self, round_no, reason_key):
        return 0

    def record_failure(self, failure):
        pass


def _ok_exec(package: TaskPackage):
    from kfpa.schemas import ExecResult
    result = ExecResult(ok=True, results={c.cid: True for c in package.acceptance})
    return result

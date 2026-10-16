"""R-30 交接改写：`gates.py` / `pipeline.py` 的纯逻辑分支测试（镜鉴独立复核版）。

来源：长流 `benchmarks/cov/stub_core_gates.py` @ `5acfc4e`（65 项 `expect()` 桩）。
**改写口径（R-30「不要原样照收」）**：

1. 断言全部改成 pytest，失败即单独报错——长流的 `expect()` 收集器会把多条失败压成一行，
   复现时看不出是哪条；这里一条用例一个失败点。
2. 期望值按口径写死（D1-1 上限 20 / D1-2 兜底关键词 / D1-3 判定顺序 / D1-5 升级阈值 /
   D3-3.3 G7-G8），不写「实现返回什么就断什么」。
3. 补了长流桩没有的攻击面：`cap_tickets` 恰好 20 / 21 的边界、`_parse_impact` 空串、
   `_resolve_checkpointer` 三档（None/auto/显式路径）、G7 报告的「无 num_statements」退化、
   `_log` 非 UTF-8 流降级、injection 与 G8 两条终态的交叉。

**为什么必须 in-process**：A20 的覆盖率由 clean env 的 `pytest --cov` 采集，而 P0/P1/P2 台架是
**CLI 子进程**驱动——子进程里跑的行不会记到 pytest-cov 上，`gates.py` 因此长期停在 35.8%。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

from kfpa import eval as eval_mod
from kfpa import executor as ex_mod
from kfpa import gates, pipeline
from kfpa import store as st_mod
from kfpa.schemas import (
    AcceptanceCriterion,
    CandidateStatus,
    CoverageReport,
    ExecResult,
    GateReport,
    GateState,
    KnowledgeCandidate,
    QATicket,
    RoundState,
    TaskPackage,
    TicketImpact,
    TicketStatus,
    package_hash,
)

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 桩


class _Store:
    """P0 七表 + P1（ledger/knowledge）+ P2（reviews）的最小桩（不碰真库）。"""

    def __init__(self) -> None:
        self.rounds: dict = {}
        self.tickets: dict = {}
        self.packages: dict = {}
        self.gate_reports: list = []
        self.failures: list = []
        self.snapshots: list = []
        self.ledger_entries: list = []
        self.candidates: dict = {}
        self.reviews: list = []
        self.raise_on_candidates = False

    # -- P0
    def init_schema(self) -> None: ...   # 桩：不落库（run_round 进图前会调）

    def upsert_round(self, r):
        self.rounds[r.round_no] = r
        return r

    def get_round(self, n):
        return self.rounds.get(n)

    def set_round_state(self, n, s):
        if n in self.rounds:
            self.rounds[n].state = s

    def add_ticket(self, t):
        self.tickets[t.ticket_id] = t
        return t

    def get_ticket(self, tid):
        return self.tickets.get(tid)

    def list_tickets(self, round_no=None, status=None, impact=None):
        out = [t for t in self.tickets.values() if round_no is None or t.round_no == round_no]
        if status is not None:
            out = [t for t in out if t.status is status]
        if impact is not None:
            out = [t for t in out if t.impact is impact]
        return sorted(out, key=lambda t: t.ticket_id)

    def close_ticket(self, tid, answer):
        t = self.tickets[tid]
        t.status, t.answer = TicketStatus.CLOSED, answer
        return t

    def list_open_high_tickets(self, n):
        return [t for t in self.list_tickets(round_no=n) if t.is_open_high()]

    def save_package(self, p):
        self.packages[p.round_no] = p
        return package_hash(p)

    def get_package(self, n):
        return self.packages.get(n)

    def save_gate_report(self, r):
        self.gate_reports.append(r)

    def list_gate_reports(self, n):
        return [r for r in self.gate_reports if r.round_no == n]

    def record_failure(self, f):
        self.failures.append(f)

    def list_avoid_patterns(self, n=None):
        return [f.avoid_pattern for f in self.failures if n is None or f.round_no == n]

    def count_same_failure(self, n, key):
        return sum(1 for f in self.failures if f.round_no == n and f.reason_key == key)

    def save_snapshot(self, s):
        self.snapshots.append(s)

    def list_snapshots(self):
        return list(self.snapshots)

    # -- P1
    def add_ledger_entry(self, e):
        self.ledger_entries.append(e)

    def list_ledger(self, n=None):
        return [e for e in self.ledger_entries if n is None or e.round_no == n]

    def list_candidates(self, status=None):
        if self.raise_on_candidates:
            raise RuntimeError("桩故障：候选表不可读")
        return list(self.candidates.values())

    def add_candidate(self, c):
        self.candidates[c.cid] = c

    def get_candidate(self, cid):
        return self.candidates.get(cid)

    def promote_candidate(self, cid, n):
        c = self.candidates[cid]
        if n not in c.reused_by:
            c.reused_by.append(n)
        c.status = CandidateStatus.PROMOTED
        return c

    # -- P2
    def add_review_report(self, r):
        self.reviews.append(r)

    def list_review_reports(self, round_no=None):
        return [r for r in self.reviews if round_no is None or r.round_no == round_no]


class _LLM:
    """ARCHITECT 按 marker 出票/出包；REVIEWER 按剧本给 pass/fail/乱码。"""

    def __init__(self, reviewer: str = "pass", package_extra: str = "") -> None:
        self.reviewer = reviewer
        self.package_extra = package_extra
        self.roles: list[str] = []

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        self.roles.append(role)
        if role == "ARCHITECT":
            if "KFPA_TASK=qa_tickets" in system:
                return json.dumps([{
                    "ticket_id": "T-001", "location": "§1", "option_a": "A", "option_b": "B",
                    "architect_preference": "A", "impact": "medium",
                }], ensure_ascii=False)
            if "KFPA_TASK=task_package" in system:
                boundaries = ["边界一"]
                if self.package_extra:
                    boundaries.append(self.package_extra)
                return json.dumps({
                    "feature": "cov", "goal": "覆盖率补桩用任务包",
                    "acceptance": [{"cid": "AC1", "text": "断言一"}],
                    "boundaries": boundaries, "interface_contract": ["IF 一"],
                    "prohibitions": ["禁止一"], "feedback": [],
                }, ensure_ascii=False)
            raise AssertionError(f"未知 ARCHITECT marker：{system[:60]}")
        if role == "REVIEWER":
            if self.reviewer == "garbled":
                return "不是 JSON"
            if self.reviewer == "fail":
                return json.dumps({"verdict": "fail", "findings": [
                    {"severity": "major", "location": "段4", "issue": "缺错误码",
                     "criterion": "AC1"}]}, ensure_ascii=False)
            return json.dumps({"verdict": "pass", "findings": []}, ensure_ascii=False)
        raise AssertionError(f"未知角色：{role}")


class _FlakyExecutor:
    """每次尝试换一种 error_category —— 专打 attempts_exhausted 终态。"""

    name = "flaky"

    def __init__(self) -> None:
        self.i = 0
        self.categories = ["test_fail", "compile", "apply_failed"]

    def apply(self, package: TaskPackage, workdir) -> ExecResult:
        cat = self.categories[self.i % len(self.categories)]
        self.i += 1
        cid = (package.acceptance or [None])[0]
        return ExecResult(ok=False, error_category=cat,
                          failed_criteria=[cid.cid if cid else "AC1"],
                          error=f"桩第 {self.i} 次：{cat}",
                          results={c.cid: False for c in package.acceptance},
                          executor=self.name)


def _pkg(round_no: int = 1, **overrides) -> TaskPackage:
    base = dict(round_no=round_no, feature="f", goal="g",
                acceptance=[AcceptanceCriterion("AC1", "t")],
                boundaries=["b"], interface_contract=["i"], prohibitions=["p"], feedback=[])
    base.update(overrides)
    return TaskPackage(**base)


def _ok_exec(p: TaskPackage, **kw) -> ExecResult:
    return ExecResult(ok=True, results={c.cid: True for c in p.acceptance}, **kw)


@pytest.fixture()
def store() -> _Store:
    return _Store()


@pytest.fixture()
def design_text() -> str:
    return (ROOT / "examples" / "design-r1.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ gates：归一化纯函数


def test_normalize_category_keeps_whitelist_and_maps_rest_to_other():
    for legal in ("apply_failed", "compile", "test_fail", "missing_section",
                  "unmapped_criterion", "coverage", "other"):
        assert gates.normalize_category(legal) == legal
    assert gates.normalize_category("不存在") == "other"
    assert gates.normalize_category(None) == "other"
    assert gates.normalize_category("") == "other"


def test_normalize_target_strips_volumes_and_epoch_digits():
    # 盘符/反斜杠/大小写/行号都要收敛成同一个 target
    assert gates.normalize_target(r" Src\A.PY:12 ") == "src/a.py"
    assert gates.normalize_target("src/a.py:999") == "src/a.py"
    # 10 位以上纯数字（epoch）视为噪声；空值归 round
    assert gates.normalize_target("x 1700000000") == "x"
    assert gates.normalize_target("") == "round"
    assert gates.normalize_target(None) == "round"


def test_reason_key_is_stable_and_case_insensitive():
    assert gates.make_reason_key("TEST_FAIL", " A1:12 ") == gates.make_reason_key("test_fail", "a1")
    assert len(gates.make_reason_key("other", "x")) == 12
    assert gates.make_reason_key("compile", "a.py") != gates.make_reason_key("test_fail", "a.py")


def test_avoid_pattern_uses_fixed_sentence():
    got = gates.make_avoid_pattern("coverage", "a.py", "boom")
    assert got.startswith("AVOID[coverage] a.py：上次失败——boom")


def test_target_of_falls_back_criteria_then_dir_then_round():
    assert gates.target_of(ExecResult(ok=False, failed_criteria=["A1"])) == "A1"
    assert gates.target_of(ExecResult(ok=False, files_changed=["out/x.py"])) == "out"
    assert gates.target_of(ExecResult(ok=False)) == "round"


# ------------------------------------------------------------------ G7：覆盖率报告解析


def _write_cov(tmp: Path, name: str, payload: dict) -> Path:
    p = tmp / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_g7_scope_keeps_only_core_files_and_weights_by_statements(tmp_path):
    wl = "src/kfpa/pipeline.py"
    path = _write_cov(tmp_path, "cov.json", {
        "totals": {"percent_covered": 50.0},
        "files": {
            wl: {"summary": {"percent_covered": 90.0, "num_statements": 100, "covered_lines": 90}},
            "src/kfpa/ui/x.py": {"summary": {"percent_covered": 0.0, "num_statements": 50,
                                             "covered_lines": 0}},
        },
        "critical_paths_ok": True,
    })
    rep = gates.load_coverage_report(path)
    assert set(rep.files) == {wl}          # ui/** 不算门槛
    assert rep.line_rate == pytest.approx(0.9)   # 加权 = 90/100，不是逐文件平均
    assert rep.critical_paths_ok is True


def test_g7_scope_recognises_store_and_rejects_ui():
    assert gates.in_core_coverage_scope("src/kfpa/store/sqlite.py") is True
    assert gates.in_core_coverage_scope("src/kfpa/ui/x.py") is False


def test_g7_falls_back_to_totals_when_report_has_no_core_file(tmp_path):
    # R-22.4 的防假红：一份无关报告不许被当成「白名单 0%」
    path = _write_cov(tmp_path, "none.json", {
        "totals": {"percent_covered": 77.0},
        "files": {"src/kfpa/ui/x.py": {"summary": {"percent_covered": 10.0}}},
    })
    rep = gates.load_coverage_report(path)
    assert rep.line_rate == pytest.approx(0.77)
    assert rep.critical_paths_ok is False   # 缺字段 → False，不编造全绿


def test_g7_without_statements_falls_back_to_file_average_with_explicit_scope(tmp_path):
    wl = "src/kfpa/pipeline.py"
    path = _write_cov(tmp_path, "pct.json", {
        "totals": {"percent_covered": 88.0},
        "files": {wl: {"summary": {"percent_covered": 88.0}}},
    })
    rep = gates.load_coverage_report(path, critical_paths_ok=True, scope=(wl,))
    assert rep.line_rate == pytest.approx(0.88)
    assert rep.critical_paths_ok is True


def test_g7_missing_report_raises_chinese_gates_error(tmp_path):
    with pytest.raises(gates.GatesError) as exc:
        gates.load_coverage_report(tmp_path / "nope.json")
    assert "覆盖率报告不存在" in str(exc.value)


def test_g7_broken_json_raises_chinese_gates_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(gates.GatesError) as exc:
        gates.load_coverage_report(bad)
    assert "无法解析" in str(exc.value)


# ------------------------------------------------------------------ G8：评审解析与折算


def test_parse_reviews_accepts_four_shapes_and_rejects_junk():
    assert gates.parse_reviews('```json\n{"reviews":[{"severity":"info"}]}\n```') == [
        {"severity": "info"}]
    assert gates.parse_reviews([{"severity": "major"}]) == [{"severity": "major"}]
    assert gates.parse_reviews({"items": [{"severity": "minor"}]}) == [{"severity": "minor"}]
    assert gates.parse_reviews("不是 JSON") == []
    assert gates.parse_reviews({"nothing": 1}) == []


def test_fold_reviews_empty_and_unparsable_input():
    assert gates.fold_reviews(None) == ([], [])
    blocking, reasons = gates.fold_reviews("不是 JSON")
    assert blocking == [] and any("解析失败" in r for r in reasons)


def test_fold_reviews_splits_blocking_from_records():
    blocking, reasons = gates.fold_reviews([
        {"severity": "major", "criterion": "AC1", "issue": "阻塞"},
        {"severity": "major", "issue": "无 criterion"},
        {"severity": "minor", "issue": "小问题"},
        {"severity": "info", "issue": "提示"},
    ])
    assert blocking == [("AC1", "other")]   # 只有 major + criterion 才是阻塞
    assert len(reasons) == 4                # 四条都要留痕


# ------------------------------------------------------------------ G5/G8 对抗复核

#: 长流（实现方）点名要求复核的两条：「G5 同因第二次升级」「G8 只记 reasons」——
#: 这两处最容易按实现写松（只断「第二次变 BLOCKED」「minor 不改三态」就放过），
#: 所以这里按**判据的反面**各打一组：不该升级的必须不升级、不该阻塞的必须不阻塞。


def test_g5_different_reason_same_round_does_not_escalate():
    """G5 按 `reason_key` 判同类：换了原因（哪怕同轮第二次）也不许升级。"""
    st = _Store()
    first = gates.evaluate(31, _pkg(), ExecResult(ok=False, error_category="test_fail",
                                                  failed_criteria=["AC1"], error="炸"), st)
    second = gates.evaluate(31, _pkg(), ExecResult(ok=False, error_category="compile",
                                                   error="编译炸", files_changed=["out/x.py"]), st)
    assert first.reason_key != second.reason_key
    assert first.state is GateState.NEEDS_FIX
    assert second.state is GateState.NEEDS_FIX          # 不同因 → 仍可重试
    assert second.escalated is False and second.same_reason_count == 0


def test_g5_same_reason_different_round_does_not_escalate(tmp_path):
    """计数按轮次隔离：同一原因跨轮各第一次都要给 NEEDS_FIX，不许累加成 BLOCKED。"""
    store = st_mod.Store(str(tmp_path / "g5.db"))
    store.init_schema()
    bad = ExecResult(ok=False, error_category="test_fail", failed_criteria=["AC1"], error="炸")
    r32 = gates.evaluate(32, _pkg(), bad, store)
    r33 = gates.evaluate(33, _pkg(), bad, store)
    assert r32.state is GateState.NEEDS_FIX
    assert r33.state is GateState.NEEDS_FIX
    assert r33.same_reason_count == 0 and r33.escalated is False
    store.close()


def test_g5_counter_and_escalation_field_semantics():
    """第二次同类 → BLOCKED，且 `same_reason_count` 是「历史次数、不含本次」。"""
    st = _Store()
    bad = ExecResult(ok=False, error_category="test_fail", failed_criteria=["AC1"], error="炸")
    r1 = gates.evaluate(34, _pkg(), bad, st)
    r2 = gates.evaluate(34, _pkg(), bad, st)
    r3 = gates.evaluate(34, _pkg(), bad, st)
    assert (r1.state, r1.escalated, r1.same_reason_count) == (GateState.NEEDS_FIX, False, 0)
    assert (r2.state, r2.escalated, r2.same_reason_count) == (GateState.BLOCKED, True, 1)
    assert (r3.state, r3.escalated, r3.same_reason_count) == (GateState.BLOCKED, True, 2)
    assert all(r.reason_key == r1.reason_key for r in (r2, r3))


def test_g8_minor_with_criterion_must_not_block():
    """阻塞判据是 `severity in {major,blocker}` **且**有 criterion —— 不是「有 criterion 就阻塞」。"""
    rep = gates.evaluate(35, _pkg(), _ok_exec(_pkg()), _Store(),
                         reviews=[{"severity": "minor", "criterion": "AC1", "location": "x",
                                   "issue": "措辞"}])
    assert rep.state is GateState.PASS
    assert rep.failing_criteria == []
    assert any("G8" in r for r in rep.reasons)


def test_g8_major_with_blank_criterion_must_not_block():
    """criterion 只有空白 → 定位不到断言 → 不阻塞，但仍要留痕。"""
    rep = gates.evaluate(36, _pkg(), _ok_exec(_pkg()), _Store(),
                         reviews=[{"severity": "major", "criterion": "   ", "location": "段4",
                                   "issue": "缺错误码"}])
    assert rep.state is GateState.PASS
    assert rep.failing_criteria == []
    assert any("无 criterion" in r for r in rep.reasons)


def test_g8_blocker_severity_is_treated_as_blocking():
    """`blocker` 与 `major` 同级（D3-3.3 表里两个都算阻塞）。"""
    rep = gates.evaluate(37, _pkg(), _ok_exec(_pkg()), _Store(),
                         reviews=[{"severity": "blocker", "criterion": "AC1", "location": "段4",
                                   "issue": "接口不可用"}])
    assert rep.state is GateState.NEEDS_FIX
    assert rep.failing_criteria == ["AC1"]


def test_g8_is_short_circuited_when_an_earlier_gate_already_failed():
    """D1-3 判定顺序是**短路**的：G1–G4 已有失败时不再折算 G8。

    这条是我复核时长流没写到的边界：我原以为「G8 只记 reasons」在任何情况下都成立，
    实测发现既有 G2 失败时评审条目根本不进 reasons。按 D1-3 这是**对的**（G7 也是同一规则），
    这里把短路语义钉死，免得以后有人「顺手」把它改成总是折算。
    评审留痕不受影响：pipeline 每次都 `persist_report()` 落 `reviews` 表（A22）。
    """
    st = _Store()
    failing = ExecResult(ok=False, error_category="bogus", error="炸了", files_changed=["out/x.py"])
    rep = gates.evaluate(38, _pkg(), failing, st, reviews="不是 JSON")
    assert rep.reason_key == gates.make_reason_key("other", "out")     # 仍是 G2 的键
    assert not any("G8" in r for r in rep.reasons)                     # 短路：G8 未参与
    assert rep.state is GateState.NEEDS_FIX


def test_g8_parse_failure_on_green_round_keeps_pass():
    rep = gates.evaluate(39, _pkg(), _ok_exec(_pkg()), _Store(), reviews={"nothing": 1})
    assert rep.state is GateState.PASS
    assert rep.reason_key == ""
    assert any("G8" in r for r in rep.reasons)


def test_g8_mixed_reviews_block_only_on_the_blocking_entry():
    rep = gates.evaluate(40, _pkg(), _ok_exec(_pkg()), _Store(), reviews=[
        {"severity": "minor", "location": "段1", "issue": "措辞"},
        {"severity": "major", "criterion": "AC1", "location": "段4", "issue": "缺错误码"},
        {"severity": "major", "location": "段5", "issue": "无 criterion"},
    ])
    assert rep.state is GateState.NEEDS_FIX
    assert rep.failing_criteria == ["AC1"]          # 有 criterion 的那条决定失败项
    assert len(rep.reasons) >= 3                    # 三条都留痕


def test_repeated_g8_blocker_escalates_to_blocked_via_g5():
    """G8 阻塞也会走 G5 同因升级：第二次同类评审阻塞 → BLOCKED（不许永远停在 NEEDS_FIX）。"""
    st = _Store()
    review = [{"severity": "major", "criterion": "AC1", "location": "段4", "issue": "缺错误码"}]
    r1 = gates.evaluate(41, _pkg(), _ok_exec(_pkg()), st, reviews=review)
    r2 = gates.evaluate(41, _pkg(), _ok_exec(_pkg()), st, reviews=review)
    assert r1.state is GateState.NEEDS_FIX
    assert r2.state is GateState.BLOCKED and r2.escalated is True


# ------------------------------------------------------------------ G1–G8 判定顺序


def test_g1_missing_section_needs_fix():
    rep = gates.evaluate(1, _pkg(boundaries=[]), _ok_exec(_pkg()), _Store())
    assert rep.state is GateState.NEEDS_FIX
    assert any("G1" in r for r in rep.reasons)


def test_g2_illegal_category_maps_to_other_and_target_falls_back_to_dir():
    rep = gates.evaluate(2, _pkg(), ExecResult(ok=False, error_category="bogus",
                                               error="炸了", files_changed=["out/x.py"]),
                         _Store())
    assert rep.state is GateState.NEEDS_FIX
    assert rep.reason_key == gates.make_reason_key("other", "out")


def test_g3_executor_declared_failure_needs_fix():
    rep = gates.evaluate(3, _pkg(), ExecResult(ok=True, error_category="test_fail",
                                               failed_criteria=["AC1"]), _Store())
    assert rep.state is GateState.NEEDS_FIX
    assert rep.failing_criteria == ["AC1"]


def test_g4_unmapped_criterion_needs_fix():
    rep = gates.evaluate(4, _pkg(), ExecResult(ok=True, results={}), _Store())
    assert rep.state is GateState.NEEDS_FIX
    assert rep.reason_key == gates.make_reason_key("unmapped_criterion", "AC1")


def test_g7_low_line_rate_blocks():
    rep = gates.evaluate(5, _pkg(), _ok_exec(_pkg()), _Store(),
                         coverage=CoverageReport(line_rate=0.5, files={}, critical_paths_ok=True))
    assert rep.state is GateState.NEEDS_FIX
    assert "<总行覆盖率>" in rep.failing_criteria     # 无具体文件时用占位符，不空手


def test_g7_not_green_critical_paths_blocks():
    rep = gates.evaluate(6, _pkg(), _ok_exec(_pkg()), _Store(),
                         coverage=CoverageReport(line_rate=0.9, files={"x": 0.9},
                                                 critical_paths_ok=False))
    assert rep.state is GateState.NEEDS_FIX
    assert "critical_paths" in rep.failing_criteria


def test_g8_non_blocking_review_only_records_reasons():
    rep = gates.evaluate(7, _pkg(), _ok_exec(_pkg()), _Store(),
                         reviews=[{"severity": "minor", "location": "x", "issue": "措辞"}])
    assert rep.state is GateState.PASS
    assert any("G8" in r for r in rep.reasons)


def test_g8_major_with_criterion_blocks():
    rep = gates.evaluate(8, _pkg(), _ok_exec(_pkg()), _Store(),
                         reviews=[{"severity": "major", "criterion": "AC1", "location": "x",
                                   "issue": "阻塞"}])
    assert rep.state is GateState.NEEDS_FIX
    assert rep.failing_criteria == ["AC1"]


def test_g5_same_reason_second_time_escalates_to_blocked():
    st = _Store()
    bad = ExecResult(ok=False, error_category="test_fail", failed_criteria=["AC1"], error="炸")
    first = gates.evaluate(9, _pkg(), bad, st)
    second = gates.evaluate(9, _pkg(), bad, st)
    assert first.state is GateState.NEEDS_FIX
    assert second.state is GateState.BLOCKED
    assert second.escalated is True
    assert second.same_reason_count == 1     # 计数不含本次（阈值判定用历史次数）


def test_all_green_round_passes():
    rep = gates.evaluate(10, _pkg(), _ok_exec(_pkg()), _Store())
    assert rep.state is GateState.PASS
    assert rep.reason_key == ""


def test_exhausted_report_keeps_last_failing_criteria_and_defaults():
    st = _Store()
    last = gates.evaluate(1, _pkg(boundaries=[]), _ok_exec(_pkg()), st)
    ex1 = gates.exhausted_report(11, 3, st, last=last)
    ex2 = gates.exhausted_report(12, 1, st)
    assert ex1.state is GateState.BLOCKED
    assert ex1.failing_criteria == last.failing_criteria      # 有 last → 沿用它
    assert "attempts_exhausted" in ex2.reasons[0]             # 无 last → 自造原因


# ------------------------------------------------------------------ pipeline：票面与任务包


def _ticket(tid: str, round_no: int = 1, **kw) -> QATicket:
    base = dict(ticket_id=tid, round_no=round_no, location="§1", option_a="a", option_b="b",
                architect_preference="p", impact=TicketImpact.LOW)
    base.update(kw)
    return QATicket(**base)


def test_impact_floor_three_tiers():
    high = _ticket("T1", option_a="新增接口 A")
    med = _ticket("T2", option_a="金额口径")
    low = _ticket("T3", location="§3 措辞", option_a="写法一")
    pipeline.apply_impact_floor([high, med, low])
    assert high.impact is TicketImpact.HIGH      # 新增接口 → HIGH
    assert med.impact is TicketImpact.MEDIUM     # 金额 → MEDIUM
    assert low.impact is TicketImpact.LOW        # 措辞 → 不动


def test_impact_floor_never_downgrades_an_existing_high():
    t = _ticket("T1", option_a="写法一", impact=TicketImpact.HIGH)
    pipeline.apply_impact_floor([t])
    assert t.impact is TicketImpact.HIGH


def test_cap_tickets_exactly_20_returns_input_unchanged():
    twenty = [_ticket(f"T{i:03d}") for i in range(20)]
    assert pipeline.cap_tickets(1, twenty) == twenty


def test_cap_tickets_21_lows_merge_into_one_summary():
    many = [_ticket(f"T{i:03d}", location=f"§{i}", option_a="措辞") for i in range(21)]
    capped = pipeline.cap_tickets(1, many)
    assert len(capped) == 1
    assert "汇总" in capped[0].location


def test_cap_tickets_keeps_high_and_medium_when_over_limit():
    many = [_ticket(f"T{i:03d}", location=f"§{i}", option_a="措辞") for i in range(19)]
    many.append(_ticket("T-HIGH", option_a="新增接口 A", impact=TicketImpact.HIGH))
    many.append(_ticket("T-MED", option_a="金额口径", impact=TicketImpact.MEDIUM))
    capped = pipeline.cap_tickets(1, many)
    ids = {t.ticket_id for t in capped}
    assert {"T-HIGH", "T-MED"} <= ids       # 高/中影响不许被合并掉
    assert any("汇总" in t.location for t in capped)   # LOW 合并成一条


def test_generate_tickets_assigns_round_prefixed_ids():
    tickets = pipeline.generate_tickets(1, "设计", _LLM())
    assert [t.ticket_id for t in tickets] == ["R1-001"]
    assert tickets[0].architect_preference == "A"


def test_generate_tickets_defaults_missing_impact_to_medium():
    tickets = pipeline.generate_tickets(2, "设计", _LLM())
    assert tickets and tickets[0].impact is TicketImpact.MEDIUM


def test_build_task_package_rejects_open_high_ticket(design_text):
    open_high = _ticket("R1-009", impact=TicketImpact.HIGH)
    with pytest.raises(pipeline.OpenHighTicketsError) as exc:
        pipeline.build_task_package(1, design_text, [open_high], _LLM(), [])
    assert "KFPA-BLOCKED" in str(exc.value)


def test_build_task_package_injects_avoid_patterns_into_feedback_and_prohibitions(design_text):
    t = _ticket("R3-001", round_no=3, option_a="解释 A", impact=TicketImpact.MEDIUM)
    pkg = pipeline.build_task_package(3, design_text, [t], _LLM(),
                                      ["AVOID[test_fail] a1：上次失败——boom"])
    assert any(x.startswith("AVOID[") for x in pkg.feedback)
    assert any("规避历史失败" in x for x in pkg.prohibitions)


def test_render_task_package_has_six_sections_and_review_prompt_names_reviewer(design_text):
    t = _ticket("R3-001", round_no=3, option_a="解释 A", impact=TicketImpact.MEDIUM)
    pkg = pipeline.build_task_package(3, design_text, [t], _LLM(), [])
    assert "## 6. 回灌区" in pipeline.render_task_package(pkg)
    prompt = pipeline.build_review_prompt(pkg, _ok_exec(pkg),
                                          GateReport(round_no=3, state=GateState.PASS))
    assert "REVIEWER" in prompt


# ------------------------------------------------------------------ pipeline：解析边界与基础设施


@pytest.mark.parametrize("bad", ["不是 JSON", "```json\n{坏的}\n```"])
def test_extract_json_rejects_unparsable_input(bad):
    with pytest.raises(pipeline.PipelineError):
        pipeline._extract_json(bad)


def test_extract_json_tolerates_fences_and_surrounding_text():
    assert pipeline._extract_json('前缀 {"a":1} 后缀') == {"a": 1}
    assert pipeline._extract_json('```json\n[{"a":1}]\n```') == [{"a": 1}]


def test_first_json_object_rejects_empty_list():
    with pytest.raises(pipeline.PipelineError):
        pipeline._first_json_object("[]")


def test_ticket_items_returns_empty_for_non_container_shapes():
    assert pipeline._ticket_items('"字符串"') == []
    assert pipeline._ticket_items('{"tickets":1}') == []


def test_as_str_list_three_inputs():
    assert pipeline._as_str_list("a") == ["a"]
    assert pipeline._as_str_list(["a", "", "b"]) == ["a", "b"]   # 空串丢掉
    assert pipeline._as_str_list(123) == ["123"]                 # 非字符串也转，不丢信息


def test_parse_impact_aliases_and_default():
    assert pipeline._parse_impact("HIGH") is TicketImpact.HIGH
    assert pipeline._parse_impact("低") is TicketImpact.LOW
    assert pipeline._parse_impact("怪") is TicketImpact.MEDIUM
    assert pipeline._parse_impact("") is TicketImpact.MEDIUM     # 空串不许抛


def test_checkpoint_path_and_resolver_three_modes(tmp_path):
    assert pipeline.default_checkpoint_path("runs/round-1").name == pipeline.CHECKPOINT_DB_NAME
    assert pipeline.default_checkpoint_path("runs/round-1") == \
        Path("runs/round-1") / "checkpoints.sqlite"
    assert pipeline._resolve_checkpointer(None, 1, tmp_path) is None        # None → 不挂
    assert pipeline._resolve_checkpointer(str(tmp_path / "cp.sqlite"), 1, tmp_path) is not None
    edge = object()
    assert pipeline._resolve_checkpointer(edge, 1, tmp_path) is edge        # 对象原样透传
    assert pipeline.make_checkpointer(tmp_path / "cp2.sqlite") is not None


def test_git_helpers_degrade_outside_repo(tmp_path):
    assert pipeline._git_sha(tmp_path) == ""
    assert pipeline._git_tag(tmp_path, "t", "m") is False


def test_default_roles_returns_dict_or_empty(tmp_path):
    assert isinstance(pipeline._default_roles(), dict)


def test_log_degrades_on_non_utf8_stream(monkeypatch):
    class _BadStream(io.StringIO):
        def write(self, s):  # noqa: ANN001
            raise UnicodeEncodeError("cp1252", "x", 0, 1, "boom")

    monkeypatch.setattr(sys, "stderr", _BadStream())
    pipeline._log("中文日志在非 UTF-8 流下必须降级，不许崩流水线")   # 不抛就算过


# ------------------------------------------------------------------ pipeline：轮次编排四条终态


def _round(round_no: int, design_text: str, workdir: Path, store: _Store, **kw):
    return pipeline.run_round(round_no, design_text, "examples/design-r1.md", store=store,
                              workdir=workdir, checkpointer=None, **kw)


def test_round_pass_state_snapshots_persists_review_and_files_changed(tmp_path, design_text):
    st = _Store()
    r = _round(20, design_text, tmp_path / "pass", st, llm=_LLM(),
               executor=ex_mod.MockExecutor())
    assert r.state is RoundState.SNAPSHOTTED
    assert len(st.snapshots) == 1
    assert len(st.reviews) == 1                          # P2：评审留痕
    assert (tmp_path / "pass" / "files_changed.json").is_file()


def test_round_with_open_high_ticket_stops_before_package(tmp_path, design_text):
    st = _Store()
    st.add_ticket(_ticket("R21-999", round_no=21, impact=TicketImpact.HIGH))
    r = _round(21, design_text, tmp_path / "high", st, llm=_LLM(),
               executor=ex_mod.MockExecutor())
    assert r.state is RoundState.TICKETS_OPEN
    assert r.task_package is None                        # 不出包
    assert pipeline.BLOCKED_MARK in r.message


def test_round_review_major_escalates_to_blocked(tmp_path, design_text):
    st = _Store()
    r = _round(22, design_text, tmp_path / "bad", st, llm=_LLM(reviewer="fail"),
               executor=ex_mod.MockExecutor())
    assert r.state is RoundState.BLOCKED
    assert r.gate.escalated is True
    assert any("G8" in x for x in r.gate.reasons)


def test_round_distinct_reasons_exhaust_to_blocked(tmp_path, design_text):
    st = _Store()
    r = _round(23, design_text, tmp_path / "ex", st, llm=_LLM(),
               executor=_FlakyExecutor(), max_attempts=3)
    assert r.state is RoundState.BLOCKED
    assert "attempts_exhausted" in r.gate.reasons[0]


def test_round_garbled_review_does_not_change_gate_state(tmp_path, design_text):
    st = _Store()
    r = _round(24, design_text, tmp_path / "garbled", st, llm=_LLM(reviewer="garbled"),
               executor=ex_mod.MockExecutor())
    assert r.state is RoundState.SNAPSHOTTED             # R-5：解析失败不改三态
    assert st.reviews[0].parse_ok is False


def test_round_promotes_reused_knowledge_candidate(tmp_path, design_text):
    st = _Store()
    st.add_candidate(KnowledgeCandidate(cid="K1-001", round_no=1, kind="success",
                                        content="禁止一", confidence=0.9))
    _round(25, design_text, tmp_path / "reuse", st, llm=_LLM(), executor=ex_mod.MockExecutor())
    got = st.get_candidate("K1-001")
    assert got.status is CandidateStatus.PROMOTED
    assert got.reused_by == [25]


def test_round_sedimentation_error_only_warns(tmp_path, design_text):
    st = _Store()
    st.raise_on_candidates = True
    r = _round(26, design_text, tmp_path / "raise", st, llm=_LLM(),
               executor=ex_mod.MockExecutor())
    assert r.state is RoundState.SNAPSHOTTED             # 沉淀表炸了也不许阻塞快照


def test_run_round_via_graph_is_equivalent_entry(tmp_path, design_text):
    r = pipeline.run_round_via_graph(27, design_text, "examples/design-r1.md", store=_Store(),
                                     llm=_LLM(), executor=ex_mod.MockExecutor(),
                                     workdir=tmp_path / "alias", checkpointer=None)
    assert r.state is RoundState.SNAPSHOTTED


def test_round_injection_test_fail_escalates_on_second_same_reason(tmp_path, design_text):
    st = _Store()
    r = _round(28, design_text, tmp_path / "inj", st, llm=_LLM(),
               executor=ex_mod.MockExecutor(), injection="test_fail")
    assert r.state is RoundState.BLOCKED


def test_ledger_records_only_real_calls(tmp_path, design_text):
    """A11 口径：账本只记**实际发生**的调用（mock 路径只该有 ARCHITECT，外加 REVIEWER）。"""
    st = _Store()
    _round(29, design_text, tmp_path / "ledger", st, llm=_LLM(),
           executor=ex_mod.MockExecutor())
    roles = {e.role for e in st.ledger_entries}
    assert roles == {"ARCHITECT", "REVIEWER"}
    assert all(e.round_no == 29 for e in st.ledger_entries)


# ------------------------------------------------------------------ eval CLI（A21 入口）


def test_eval_cli_missing_seed_returns_4(tmp_path, capsys):
    parser = argparse.ArgumentParser(prog="kfpa")
    subs = parser.add_subparsers(dest="cmd")
    eval_mod.register(subs)
    args = parser.parse_args(["eval", "review", "--seed", str(tmp_path / "missing.jsonl")])
    rc = args.handler(args)
    assert rc == 4
    assert "种子集不存在" in capsys.readouterr().out


def test_eval_seed_loader_reads_repo_corpus():
    samples = eval_mod.load_seed(ROOT / "benchmarks" / "review" / "seed.jsonl")
    assert len(samples) >= 20

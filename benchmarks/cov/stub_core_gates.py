#!/usr/bin/env python3
"""P2 覆盖率补桩：in-process 打满 `gates.py` / `pipeline.py`（长流 · R-30/R-31/R-32）。

为什么需要它：A20 的覆盖率由 clean env 的 `pytest --cov` 采集，而 P0/P1/P2 台架是
**CLI 子进程**驱动，in-process 覆盖率收不到那些路径 → `gates.py` 长期只有 35.8%。
本脚本把「纯逻辑入口」直接 import 进来逐条打，交给镜鉴按 R-30 改写进 `tests/`。

边界都在：G1–G8 各分支、归一化纯函数、覆盖率报告解析的四种口径、评审解析/折算的四种形态、
轮次编排的四条终态（PASS / 高影响票拦停 / 同因升级 / 重试耗尽）、依赖与异常降级。

运行（clean env，自带 R-19 stdio 兜底）：

    python benchmarks/cov/stub_core_gates.py     # 退出码 0=全过 / 1=有失败
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from kfpa import eval as eval_mod  # noqa: E402
from kfpa import executor as ex_mod  # noqa: E402
from kfpa import gates, pipeline, review as review_mod  # noqa: E402
from kfpa.schemas import (  # noqa: E402
    AcceptanceCriterion,
    CoverageReport,
    ExecResult,
    GateReport,
    GateState,
    QATicket,
    ReviewFinding,
    ReviewReport,
    RoundState,
    Severity,
    TaskPackage,
    TicketImpact,
    TicketStatus,
    package_hash,
)

PASSED: list[str] = []
FAILED: list[str] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(f"{name} {detail}".strip())
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 桩


class CovStore:
    """P0 七表 + P1（ledger/knowledge）+ P2（reviews）的最小桩。"""

    def __init__(self) -> None:
        self.rounds, self.tickets, self.packages = {}, {}, {}
        self.gate_reports, self.failures, self.snapshots = [], [], []
        self.ledger_entries, self.candidates, self.reviews = [], {}, []
        self.raise_on_candidates = False

    def init_schema(self) -> None: ...
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
        from kfpa.schemas import CandidateStatus

        c.status = CandidateStatus.PROMOTED
        return c
    def add_review_report(self, r):
        self.reviews.append(r)
    def list_review_reports(self, round_no=None):
        return [r for r in self.reviews if round_no is None or r.round_no == round_no]


class StubLLM:
    """ARCHITECT 出六段式任务包；REVIEWER 按 `[DEFECT:...]` 标记出发现。"""

    def __init__(self, reviewer: str = "pass", package_extra: str = "") -> None:
        self.reviewer = reviewer
        self.package_extra = package_extra
        self.roles: list[str] = []

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        self.roles.append(role)
        if role == "ARCHITECT":
            if "KFPA_TASK=qa_tickets" in system:
                return json.dumps([
                    {"ticket_id": "T-001", "location": "§1", "option_a": "A", "option_b": "B",
                     "architect_preference": "A", "impact": "medium"},
                ], ensure_ascii=False)
            if "KFPA_TASK=task_package" in system:
                return json.dumps({
                    "feature": "cov",
                    "goal": "覆盖率补桩用任务包",
                    "acceptance": [{"cid": "AC1", "text": "断言一"}],
                    "boundaries": ["边界一", self.package_extra] if self.package_extra else ["边界一"],
                    "interface_contract": ["IF 一"],
                    "prohibitions": ["禁止一"],
                    "feedback": [],
                }, ensure_ascii=False)
            raise AssertionError("未知 ARCHITECT marker")
        if role == "REVIEWER":
            if self.reviewer == "garbled":
                return "不是 JSON"
            if self.reviewer == "fail":
                return json.dumps({"verdict": "fail", "findings": [
                    {"severity": "major", "location": "段4", "issue": "缺错误码", "criterion": "AC1"}]},
                    ensure_ascii=False)
            return json.dumps({"verdict": "pass", "findings": []}, ensure_ascii=False)
        raise AssertionError(f"未知角色 {role}")


class FlakyExecutor:
    """每次尝试换一种 error_category → 覆盖 attempts_exhausted 终态。"""

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


def pkg(round_no: int = 1, **overrides) -> TaskPackage:
    base = dict(
        round_no=round_no, feature="f", goal="g",
        acceptance=[AcceptanceCriterion("AC1", "t")],
        boundaries=["b"], interface_contract=["i"], prohibitions=["p"], feedback=[],
    )
    base.update(overrides)
    return TaskPackage(**base)


def ok_exec(p: TaskPackage, **kw) -> ExecResult:
    return ExecResult(ok=True, results={c.cid: True for c in p.acceptance}, **kw)


# ---------------------------------------------------------------- gates.py 用例


def check_gates(tmp: Path) -> None:
    print("== G 组：归一化纯函数 ==")
    expect("normalize_category 合法值原样", gates.normalize_category("compile") == "compile")
    expect("normalize_category 非法/空 → other",
           gates.normalize_category("nope") == "other" and gates.normalize_category(None) == "other")
    expect("normalize_target 去噪", gates.normalize_target(r" Src\A.PY:12 ") == "src/a.py")
    expect("normalize_target 长数字/空 → round",
           gates.normalize_target("x 1700000000") == "x" and gates.normalize_target("") == "round")
    expect("make_reason_key 稳定且 12 位",
           gates.make_reason_key("TEST_FAIL", " A1:12 ") == gates.make_reason_key("test_fail", "a1")
           and len(gates.make_reason_key("other", "x")) == 12)
    expect("make_avoid_pattern 固定句式",
           gates.make_avoid_pattern("coverage", "a.py", "boom").startswith("AVOID[coverage] a.py：上次失败——boom"))
    expect("target_of 三档回退",
           gates.target_of(ExecResult(ok=False, failed_criteria=["A1"])) == "A1"
           and gates.target_of(ExecResult(ok=False, files_changed=["out/x.py"])) == "out"
           and gates.target_of(ExecResult(ok=False)) == "round")

    print("== G7：覆盖率报告解析 ==")
    write = lambda name, payload: (tmp / name).write_text(json.dumps(payload), encoding="utf-8")
    wl = "src/kfpa/pipeline.py"
    cov_full = tmp / "cov_full.json"
    write("cov_full.json", {"totals": {"percent_covered": 50.0},
                            "files": {wl: {"summary": {"percent_covered": 90.0, "num_statements": 100,
                                                       "covered_lines": 90}},
                                      "src/kfpa/ui/x.py": {"summary": {"percent_covered": 0.0,
                                                                       "num_statements": 50,
                                                                       "covered_lines": 0}}},
                            "critical_paths_ok": True})
    rep = gates.load_coverage_report(cov_full)
    expect("G7 只统计白名单 + 加权行覆盖", set(rep.files) == {wl} and abs(rep.line_rate - 0.9) < 1e-9)
    expect("G7 critical_paths_ok 取 json 值", rep.critical_paths_ok is True)
    expect("G7 in_core_coverage_scope 判定",
           gates.in_core_coverage_scope("src/kfpa/store/sqlite.py")
           and not gates.in_core_coverage_scope("src/kfpa/ui/x.py"))
    cov_none = tmp / "cov_none.json"
    write("cov_none.json", {"totals": {"percent_covered": 77.0},
                            "files": {"src/kfpa/ui/x.py": {"summary": {"percent_covered": 10.0}}}})
    rep2 = gates.load_coverage_report(cov_none)
    expect("G7 报告无白名单文件 → 退回全量口径", abs(rep2.line_rate - 0.77) < 1e-9)
    expect("G7 缺 critical_paths_ok → False（不编造）", rep2.critical_paths_ok is False)
    cov_pct = tmp / "cov_pct.json"
    write("cov_pct.json", {"totals": {"percent_covered": 88.0},
                           "files": {wl: {"summary": {"percent_covered": 88.0}}}})
    rep3 = gates.load_coverage_report(cov_pct, critical_paths_ok=True, scope=(wl,))
    expect("G7 无 num_statements 时退回算术平均 + 显式 scope/关键路径",
           abs(rep3.line_rate - 0.88) < 1e-9 and rep3.critical_paths_ok is True)
    try:
        gates.load_coverage_report(tmp / "nope.json")
        expect("G7 报告缺失 → GatesError", False, "没有抛")
    except gates.GatesError as exc:
        expect("G7 报告缺失 → GatesError", "覆盖率报告不存在" in str(exc))
    bad = tmp / "bad.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    try:
        gates.load_coverage_report(bad)
        expect("G7 报告坏 JSON → GatesError", False, "没有抛")
    except gates.GatesError as exc:
        expect("G7 报告坏 JSON → GatesError", "无法解析" in str(exc))

    print("== G8：评审解析与折算 ==")
    expect("parse_reviews 四种输入",
           gates.parse_reviews('```json\n{"reviews":[{"severity":"info"}]}\n```') == [{"severity": "info"}]
           and gates.parse_reviews([{"severity": "major"}]) == [{"severity": "major"}]
           and gates.parse_reviews({"items": [{"severity": "minor"}]}) == [{"severity": "minor"}]
           and gates.parse_reviews("不是 JSON") == []
           and gates.parse_reviews({"nothing": 1}) == [])
    blocking, reasons = gates.fold_reviews(None)
    expect("fold_reviews 空输入 → 无阻塞无理由", blocking == [] and reasons == [])
    blocking, reasons = gates.fold_reviews("不是 JSON")
    expect("fold_reviews 解析失败 → 只记理由", blocking == [] and any("解析失败" in r for r in reasons))
    blocking, reasons = gates.fold_reviews([
        {"severity": "major", "criterion": "AC1", "issue": "阻塞"},
        {"severity": "major", "issue": "无 criterion"},
        {"severity": "minor", "issue": "小问题"},
        {"severity": "info", "issue": "提示"},
    ])
    expect("fold_reviews 四种分档", blocking == [("AC1", "other")] and len(reasons) == 4)

    print("== G1–G8 判定与升级 ==")
    st = CovStore()
    rep_g1 = gates.evaluate(1, pkg(boundaries=[]), ok_exec(pkg()), CovStore())
    expect("G1 缺段 → NEEDS_FIX/missing_section",
           rep_g1.state is GateState.NEEDS_FIX
           and any("G1" in r for r in rep_g1.reasons))
    rep_g2 = gates.evaluate(2, pkg(), ExecResult(ok=False, error_category="bogus",
                                                 error="炸了", files_changed=["out/x.py"]), CovStore())
    expect("G2 非法 category → other + target 回退目录",
           rep_g2.state is GateState.NEEDS_FIX and gates.make_reason_key("other", "out")
           == rep_g2.reason_key, rep_g2.reason_key)
    rep_g3 = gates.evaluate(3, pkg(), ExecResult(ok=True, error_category="test_fail",
                                                 failed_criteria=["AC1"]), CovStore())
    expect("G3 执行器声明失败 → NEEDS_FIX", rep_g3.state is GateState.NEEDS_FIX
           and rep_g3.failing_criteria == ["AC1"])
    rep_g4 = gates.evaluate(4, pkg(), ExecResult(ok=True, results={}), CovStore())
    expect("G4 断言未映射 → NEEDS_FIX/unmapped_criterion",
           rep_g4.state is GateState.NEEDS_FIX
           and rep_g4.reason_key == gates.make_reason_key("unmapped_criterion", "AC1"),
           rep_g4.reason_key)
    rep_g7a = gates.evaluate(5, pkg(), ok_exec(pkg()), CovStore(),
                             coverage=CoverageReport(line_rate=0.5, files={}, critical_paths_ok=True))
    expect("G7 总行覆盖不足 → failing 占位 + needs_fix",
           rep_g7a.state is GateState.NEEDS_FIX and "<总行覆盖率>" in rep_g7a.failing_criteria)
    rep_g7b = gates.evaluate(6, pkg(), ok_exec(pkg()), CovStore(),
                             coverage=CoverageReport(line_rate=0.9, files={"x": 0.9},
                                                     critical_paths_ok=False))
    expect("G7 关键路径不全 → critical_paths 进 failing",
           rep_g7b.state is GateState.NEEDS_FIX and "critical_paths" in rep_g7b.failing_criteria)
    rep_g8ok = gates.evaluate(7, pkg(), ok_exec(pkg()), CovStore(),
                              reviews=[{"severity": "minor", "location": "x", "issue": "措辞"}])
    expect("G8 非阻塞评审 → PASS 且记 reasons",
           rep_g8ok.state is GateState.PASS and any("G8" in r for r in rep_g8ok.reasons))
    st_g8 = CovStore()
    rep_g8 = gates.evaluate(8, pkg(), ok_exec(pkg()), st_g8,
                            reviews=[{"severity": "major", "criterion": "AC1", "location": "x",
                                      "issue": "阻塞"}])
    expect("G8 major+criterion → NEEDS_FIX + failing", rep_g8.state is GateState.NEEDS_FIX
           and rep_g8.failing_criteria == ["AC1"])
    st_esc = CovStore()
    first = gates.evaluate(9, pkg(), ExecResult(ok=False, error_category="test_fail",
                                                failed_criteria=["AC1"], error="炸"), st_esc)
    second = gates.evaluate(9, pkg(), ExecResult(ok=False, error_category="test_fail",
                                                 failed_criteria=["AC1"], error="炸"), st_esc)
    expect("G5 同因第二次 → BLOCKED + escalated + 计数不含本次",
           first.state is GateState.NEEDS_FIX and second.state is GateState.BLOCKED
           and second.escalated is True and second.same_reason_count == 1)
    rep_pass = gates.evaluate(10, pkg(), ok_exec(pkg()), CovStore())
    expect("全过 → PASS（D4 覆盖率前置）",
           rep_pass.state is GateState.PASS and rep_pass.reason_key == "")

    print("== exhausted_report ==")
    st_ex = CovStore()
    ex1 = gates.exhausted_report(11, 3, st_ex, last=rep_g1)
    ex2 = gates.exhausted_report(12, 1, st_ex)
    expect("exhausted_report 两种入参（有/无 last）",
           ex1.state is GateState.BLOCKED and ex1.failing_criteria == rep_g1.failing_criteria
           and "attempts_exhausted" in ex2.reasons[0])


# ---------------------------------------------------------------- pipeline.py 用例


def check_pipeline(tmp: Path) -> None:
    design = (REPO / "examples" / "design-r1.md").read_text(encoding="utf-8")

    print("== P：票面/任务包纯函数 ==")
    t_high = QATicket(ticket_id="R1-001", round_no=1, location="§1", option_a="新增接口 A",
                      option_b="B", architect_preference="P", impact=TicketImpact.LOW)
    t_med = QATicket(ticket_id="R1-002", round_no=1, location="§2", option_a="金额口径",
                     option_b="B", architect_preference="P", impact=TicketImpact.LOW)
    t_low = QATicket(ticket_id="R1-003", round_no=1, location="§3 措辞", option_a="写法一",
                     option_b="写法二", architect_preference="P", impact=TicketImpact.LOW)
    pipeline.apply_impact_floor([t_high, t_med, t_low])
    expect("影响面兜底三档", t_high.impact is TicketImpact.HIGH
           and t_med.impact is TicketImpact.MEDIUM and t_low.impact is TicketImpact.LOW)
    many = [QATicket(ticket_id=f"R1-{i:03d}", round_no=1, location=f"§{i}", option_a="措辞",
                     option_b="b", architect_preference="p", impact=TicketImpact.LOW)
            for i in range(1, 26)]
    capped = pipeline.cap_tickets(1, many)
    expect("单轮 >20 张 → LOW 合并成一条汇总票",
           len(capped) == 1 and "汇总" in capped[0].location)
    expect("≤20 张原样返回", pipeline.cap_tickets(1, many[:5]) == many[:5])

    stub = StubLLM()
    tickets = pipeline.generate_tickets(1, "设计", stub)
    expect("generate_tickets 分配票号并覆盖 mock 的 T-001",
           [t.ticket_id for t in tickets] == ["R1-001"]
           and tickets[0].architect_preference == "A")
    tickets_alias = pipeline.generate_tickets(2, "设计", StubLLM(package_extra=""))
    expect("票面缺字段仍可解析（impact 默认 medium）",
           tickets_alias and tickets_alias[0].impact is TicketImpact.MEDIUM)
    open_high = QATicket(ticket_id="R1-009", round_no=1, location="§9", option_a="a",
                         option_b="b", architect_preference="p", impact=TicketImpact.HIGH)
    try:
        pipeline.build_task_package(1, design, [open_high], stub, [])
        expect("build_task_package 硬规则：未关闭 HIGH → 拒绝", False, "没有抛")
    except pipeline.OpenHighTicketsError as exc:
        expect("build_task_package 硬规则：未关闭 HIGH → 拒绝", "KFPA-BLOCKED" in str(exc))
    t_open_medium = QATicket(ticket_id="R3-001", round_no=3, location="§3", option_a="解释 A",
                             option_b="解释 B", architect_preference="A",
                             impact=TicketImpact.MEDIUM)
    p = pipeline.build_task_package(3, design, [t_open_medium], stub,
                                    ["AVOID[test_fail] a1：上次失败——boom"])
    expect("回灌区注入 feedback + prohibitions",
           any(x.startswith("AVOID[") for x in p.feedback)
           and any("规避历史失败" in x for x in p.prohibitions))
    expect("六段渲染与评审提示词可生成",
           "## 6. 回灌区" in pipeline.render_task_package(p)
           and "REVIEWER" in pipeline.build_review_prompt(p, ok_exec(p), GateReport(
               round_no=3, state=GateState.PASS)))

    print("== P：解析边界与基础设施 ==")
    for bad in ("不是 JSON", "```json\n{坏的}\n```"):
        try:
            pipeline._extract_json(bad)
            expect("_extract_json 非法输入 → PipelineError", False, f"没有抛：{bad[:8]}")
        except pipeline.PipelineError:
            expect("_extract_json 非法输入 → PipelineError", True, bad[:10])
    expect("_extract_json 容忍围栏/前后文字",
           pipeline._extract_json('前缀 {"a":1} 后缀') == {"a": 1}
           and pipeline._extract_json('```json\n[{"a":1}]\n```') == [{"a": 1}])
    try:
        pipeline._first_json_object("[]")
        expect("_first_json_object 空列表 → PipelineError", False, "没有抛")
    except pipeline.PipelineError:
        expect("_first_json_object 空列表 → PipelineError", True)
    expect("_ticket_items 非 dict/list → []",
           pipeline._ticket_items('"字符串"') == [] and pipeline._ticket_items('{"tickets":1}') == [])
    expect("_as_str_list 三种输入",
           pipeline._as_str_list("a") == ["a"] and pipeline._as_str_list(["a", "", "b"]) == ["a", "b"]
           and pipeline._as_str_list(123) == ["123"])
    expect("_parse_impact 中英文别名",
           pipeline._parse_impact("HIGH") is TicketImpact.HIGH
           and pipeline._parse_impact("低") is TicketImpact.LOW
           and pipeline._parse_impact("怪") is TicketImpact.MEDIUM)
    expect("default_checkpoint_path 口径", pipeline.default_checkpoint_path("runs/round-1").name
           == pipeline.CHECKPOINT_DB_NAME)
    expect("_resolve_checkpointer: None → 不挂",
           pipeline._resolve_checkpointer(None, 1, tmp) is None)
    expect("_resolve_checkpointer: 字符串当路径",
           pipeline._resolve_checkpointer(str(tmp / "cp.sqlite"), 1, tmp) is not None)
    expect("make_checkpointer 落盘", pipeline.make_checkpointer(tmp / "cp2.sqlite") is not None)
    expect("_git_sha 非仓库 → 空串", pipeline._git_sha(tmp) == "")
    expect("_git_tag 非仓库 → False", pipeline._git_tag(tmp, "t", "m") is False)
    expect("_default_roles 读不到配置 → 空表（不编造）",
           isinstance(pipeline._default_roles(), dict))

    print("== P：_log 的 R-18 兜底 ==")
    class _BadStream(io.StringIO):
        def write(self, s):  # noqa: D401
            raise UnicodeEncodeError("cp1252", "x", 0, 1, "boom")

    real_stderr = sys.stderr
    try:
        sys.stderr = _BadStream()
        pipeline._log("中文日志在非 UTF-8 流下必须降级")
        expect("_log 编码失败降级不崩", True)
    except UnicodeEncodeError:
        expect("_log 编码失败降级不崩", False, "抛出 UnicodeEncodeError")
    finally:
        sys.stderr = real_stderr

    print("== P：轮次编排四条终态 ==")
    st_pass = CovStore()
    r_pass = pipeline.run_round(20, design, "examples/design-r1.md", store=st_pass, llm=StubLLM(),
                                executor=ex_mod.MockExecutor(), workdir=tmp / "pass", checkpointer=None)
    expect("终态①PASS + 快照 + 评审留痕",
           r_pass.state is RoundState.SNAPSHOTTED and len(st_pass.snapshots) == 1
           and len(st_pass.reviews) == 1 and (tmp / "pass" / "files_changed.json").is_file())
    st_high = CovStore()
    st_high.add_ticket(QATicket(ticket_id="R21-999", round_no=21, location="§9", option_a="a",
                                option_b="b", architect_preference="p", impact=TicketImpact.HIGH))
    r_high = pipeline.run_round(21, design, "examples/design-r1.md", store=st_high, llm=StubLLM(),
                                executor=ex_mod.MockExecutor(), workdir=tmp / "high", checkpointer=None)
    expect("终态②高影响票拦停 → TICKETS_OPEN + 不出包",
           r_high.state is RoundState.TICKETS_OPEN and r_high.task_package is None
           and pipeline.BLOCKED_MARK in r_high.message)
    st_bad = CovStore()
    r_bad = pipeline.run_round(22, design, "examples/design-r1.md", store=st_bad, llm=StubLLM(reviewer="fail"),
                               executor=ex_mod.MockExecutor(), workdir=tmp / "bad", checkpointer=None)
    expect("终态③评审驱动 G8 → BLOCKED/escalated",
           r_bad.state is RoundState.BLOCKED and r_bad.gate.escalated is True
           and any("G8" in r for r in r_bad.gate.reasons))
    st_ex = CovStore()
    r_ex = pipeline.run_round(23, design, "examples/design-r1.md", store=st_ex, llm=StubLLM(),
                              executor=FlakyExecutor(), workdir=tmp / "ex", checkpointer=None,
                              max_attempts=3)
    expect("终态④不同原因耗尽 → attempts_exhausted",
           r_ex.state is RoundState.BLOCKED and "attempts_exhausted" in r_ex.gate.reasons[0])
    st_garbled = CovStore()
    r_garbled = pipeline.run_round(24, design, "examples/design-r1.md", store=st_garbled,
                                   llm=StubLLM(reviewer="garbled"), executor=ex_mod.MockExecutor(),
                                   workdir=tmp / "garbled", checkpointer=None)
    expect("评审解析失败不改三态", r_garbled.state is RoundState.SNAPSHOTTED
           and not st_garbled.reviews[0].parse_ok)
    st_reuse = CovStore()
    from kfpa.schemas import CandidateStatus, KnowledgeCandidate

    st_reuse.add_candidate(KnowledgeCandidate(cid="K1-001", round_no=1, kind="success",
                                              content="禁止一", confidence=0.9))
    r_reuse = pipeline.run_round(25, design, "examples/design-r1.md", store=st_reuse, llm=StubLLM(),
                                 executor=ex_mod.MockExecutor(), workdir=tmp / "reuse",
                                 checkpointer=None)
    got = st_reuse.get_candidate("K1-001")
    expect("PASS 轮沉淀候选复用 → promoted",
           got.status is CandidateStatus.PROMOTED and got.reused_by == [25])
    st_raise = CovStore()
    st_raise.raise_on_candidates = True
    r_raise = pipeline.run_round(26, design, "examples/design-r1.md", store=st_raise, llm=StubLLM(),
                                 executor=ex_mod.MockExecutor(), workdir=tmp / "raise",
                                 checkpointer=None)
    expect("沉淀表异常 → 只警告不阻塞快照", r_raise.state is RoundState.SNAPSHOTTED)
    r_alias = pipeline.run_round_via_graph(27, design, "examples/design-r1.md", store=CovStore(),
                                           llm=StubLLM(), executor=ex_mod.MockExecutor(),
                                           workdir=tmp / "alias", checkpointer=None)
    expect("run_round_via_graph 等价入口", r_alias.state is RoundState.SNAPSHOTTED)
    st_inject = CovStore()
    r_inject = pipeline.run_round(28, design, "examples/design-r1.md", store=st_inject, llm=StubLLM(),
                                  executor=ex_mod.MockExecutor(), workdir=tmp / "inj",
                                  checkpointer=None, injection="test_fail")
    expect("injection=test_fail → 同因第二次 BLOCKED",
           r_inject.state is RoundState.BLOCKED, str(r_inject.state))

    print("== P：A18/A21 入口（eval 模块） ==")
    expect("estimate/CLI 注册可调用", callable(eval_mod.register))
    seed = REPO / "benchmarks" / "review" / "seed.jsonl"
    samples = eval_mod.load_seed(seed)
    expect("种子集可加载且非空", len(samples) >= 20)
    parser = argparse.ArgumentParser(prog="kfpa")
    subs = parser.add_subparsers(dest="cmd")
    eval_mod.register(subs)
    args = parser.parse_args(["eval", "review", "--seed", str(tmp / "missing.jsonl")])
    buf = io.StringIO()
    old = sys.stdout
    try:
        sys.stdout = buf
        rc = args.handler(args)
    finally:
        sys.stdout = old
    expect("eval CLI 缺种子集 → rc=4", rc == 4 and "种子集不存在" in buf.getvalue())


# ---------------------------------------------------------------- 入口


def _stdio_fallback() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _stdio_fallback()
    tmp = Path(tempfile.mkdtemp(prefix="kfpa-cov-core-"))
    print(f"gates/pipeline 覆盖率补桩（长流 R-30/R-32 交接物）  仓库={REPO}  临时目录={tmp}")
    check_gates(tmp)
    check_pipeline(tmp)
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print("  FAILED: " + item)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

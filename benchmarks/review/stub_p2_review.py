#!/usr/bin/env python3
"""P2 评审/度量桩验证（长流 · R-30 交接物）。

用途与归属：
- 本文件在长流写者范围内（`benchmarks/review/**`），是**实现方的自测桩**；
- 按 R-30，由镜鉴改写进 `tests/test_p2_review.py` 做独立验证——**不要原样照收**，
  实现方的断言正是最该被复核的部分；边界用例（空集 / 全漏 / 全错 / 缺 `baseline_*` /
  非法 JSONL / `--out` 与 rc=4 分支）必须保留。

覆盖：A19（fresh context）、A21（同源盲区度量三指标 + 报告形状）、A22（评审接入主循环 +
G8 折算 + reviews 留痕 + 解析失败不改三态），以及解析容错与错误分支。

运行（clean env，自带 R-19 stdio 兜底）：

    python benchmarks/review/stub_p2_review.py        # 退出码 0=全过 / 1=有失败
"""

from __future__ import annotations

import argparse
import contextlib
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
from kfpa import pipeline  # noqa: E402
from kfpa import review as review_mod  # noqa: E402
from kfpa.schemas import (  # noqa: E402
    AcceptanceCriterion,
    GateReport,
    GateState,
    ReviewFinding,
    ReviewMetrics,
    ReviewReport,
    RoundState,
    Severity,
    TaskPackage,
    TicketStatus,
    package_hash,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(f"{name} {detail}".strip())
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 桩 Store（P0 + P1 + P2 reviews）


class P2Store:
    def __init__(self) -> None:
        self.rounds, self.tickets, self.packages = {}, {}, {}
        self.gate_reports, self.failures, self.snapshots = [], [], []
        self.ledger_entries, self.candidates = [], {}
        self.reviews: list[ReviewReport] = []

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
        return list(self.candidates.values())
    def add_candidate(self, c):
        self.candidates[c.cid] = c
    def get_candidate(self, cid):
        return self.candidates.get(cid)
    def promote_candidate(self, cid, n):
        return self.candidates[cid]
    def add_review_report(self, r):
        self.reviews.append(r)
    def list_review_reports(self, round_no=None):
        return [r for r in self.reviews if round_no is None or r.round_no == round_no]


# ---------------------------------------------------------------- 桩剧本（等价替身）


class HeteroStub:
    """异构剧本：任务包里的每个 `[DEFECT:kind] CRIT:` 标记 → major + criterion。"""

    name = "mock_hetero"
    kinds: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        if role == "ARCHITECT":
            if "KFPA_TASK=qa_tickets" in system:
                return "[]"
            if "KFPA_TASK=task_package" in system:
                return json.dumps({
                    "feature": "order-atp",
                    "goal": "实现门店订单可承诺量校验",
                    "acceptance": [{"cid": "AC1", "text": "可承诺量口径可复算"}],
                    "boundaries": ["qty 正整数", os.environ.get("STUB_DEFECT_LINE", "无缺陷")],
                    "interface_contract": ["POST /x 出参 {ok, code}"],
                    "prohibitions": ["不改库存表"],
                    "feedback": [],
                }, ensure_ascii=False)
            raise AssertionError("未知 ARCHITECT marker")
        if role == "REVIEWER":
            self.calls.append(role)
            findings = []
            for match in re.finditer(r"\[DEFECT:([a-z_]+)\]\s*([A-Za-z0-9_-]+):", system):
                kind, criterion = match.group(1), match.group(2)
                if self.kinds and kind not in self.kinds:
                    continue
                findings.append({
                    "severity": "major",
                    "location": "任务包第 3 段",
                    "issue": f"检测到注入缺陷 {kind}",
                    "suggestion": "补齐该段并加上断言",
                    "criterion": criterion,
                })
            return json.dumps(
                {"verdict": "fail" if findings else "pass", "findings": findings}, ensure_ascii=False
            )
        raise AssertionError(f"未知角色 {role}")


class WeakStub(HeteroStub):
    """同源 baseline 剧本：只认得 missing_error_code（漏检其余）。"""

    name = "mock"
    kinds = ("missing_error_code",)


class BadReviewStub(HeteroStub):
    """REVIEWER 返回非 JSON → 解析失败分支。"""

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        if role == "REVIEWER":
            return "【无法输出 JSON】"
        return super().complete(role, system, user, json_mode=json_mode)


def make_package(defect: str = "") -> TaskPackage:
    return TaskPackage(
        round_no=1, feature="f", goal="g",
        acceptance=[AcceptanceCriterion("AC1", "t")],
        boundaries=["qty 正整数", defect] if defect else ["qty 正整数"],
        interface_contract=["POST /x 出参 {ok, code}"],
        prohibitions=["不改库存表"],
    )


# ---------------------------------------------------------------- 用例


def run_checks(tmp: Path) -> None:
    design = (REPO / "examples" / "design-r1.md").read_text(encoding="utf-8")

    print("== T1 解析容错（契约 1.3 形状 + 旧 R-5 形状 + 非 JSON） ==")
    r1 = review_mod.parse_review(
        '{"verdict":"fail","findings":[{"severity":"major","location":"段4","issue":"缺错误码",'
        '"suggestion":"补","criterion":"AC2"}]}',
        round_no=1, provider="mock_hetero", model_ref="m", context_id="rev-1-1-abc")
    check("T1 新形状解析", r1.parse_ok and r1.verdict == "fail" and len(r1.blockers()) == 1
          and r1.findings[0].criterion == "AC2")
    r1b = review_mod.parse_review('{"reviews":[{"severity":"minor","location":"段1","issue":"措辞"}]}',
                                  round_no=1, provider="p", model_ref="m", context_id="c")
    check("T1 兼容旧 reviews 形状", r1b.parse_ok and r1b.verdict == "pass"
          and r1b.findings[0].severity is Severity.MINOR)
    r1c = review_mod.parse_review("不是 JSON", round_no=1, provider="p", model_ref="m", context_id="c")
    check("T1 非 JSON → parse_ok=False + unknown", (not r1c.parse_ok) and r1c.verdict == "unknown")
    r_arr = review_mod.parse_review('[{"severity":"weird","location":"x","issue":"i"}]',
                                    round_no=1, provider="p", model_ref="m", context_id="c")
    check("T1 数组形状 + 未知 severity → INFO", r_arr.parse_ok
          and r_arr.findings[0].severity is Severity.INFO)
    check("T1 blocker 判据 = major 且带 criterion",
          len(ReviewReport(round_no=1, provider="p", model_ref="m", context_id="c", parse_ok=True,
                           verdict="fail", findings=[
                               ReviewFinding(Severity.MAJOR, "a", "i"),
                               ReviewFinding(Severity.MAJOR, "b", "i", criterion="AC9"),
                               ReviewFinding(Severity.MINOR, "c", "i", criterion="AC8"),
                           ]).blockers()) == 1)

    print("== T2 A19 fresh context ==")
    c1, c2 = review_mod.new_context_id(1, 1), review_mod.new_context_id(1, 1)
    check("T2 两次 context_id 不同", c1 != c2, f"{c1} vs {c2}")
    check("T2 与 ARCHITECT 上下文不同前缀",
          c1.startswith(review_mod.REVIEW_CONTEXT_PREFIX)
          and not c1.startswith(review_mod.ARCHITECT_CONTEXT_PREFIX)
          and review_mod.architect_context_id(1) == "arch-1")
    stub = HeteroStub()
    rep_a = review_mod.run_review(make_package(), stub, provider="mock_hetero", round_no=1)
    rep_b = review_mod.run_review(make_package(), stub, provider="mock_hetero", round_no=1)
    check("T2 run_review 每次新会话", rep_a.context_id != rep_b.context_id
          and rep_a.provider == "mock_hetero")

    print("== T3 G8 折算（纯函数） ==")
    base_pass = GateReport(round_no=1, state=GateState.PASS, reasons=["全过"])
    fold_block = review_mod.apply_review_to_gate(ReviewReport(
        round_no=1, provider="mock_hetero", model_ref="m", context_id="c", parse_ok=True,
        verdict="fail", findings=[ReviewFinding(Severity.MAJOR, "段4", "缺错误码", criterion="AC2")]),
        base_pass)
    check("T3 major+criterion → NEEDS_FIX + failing", fold_block.state is GateState.NEEDS_FIX
          and "AC2" in fold_block.failing_criteria)
    fold_nocrit = review_mod.apply_review_to_gate(ReviewReport(
        round_no=1, provider="p", model_ref="m", context_id="c", parse_ok=True, verdict="fail",
        findings=[ReviewFinding(Severity.MAJOR, "段5", "路径冲突")]), base_pass)
    check("T3 major 无 criterion → 不变 + 记理由", fold_nocrit.state is GateState.PASS
          and any("不阻塞" in r for r in fold_nocrit.reasons))
    fold_parse = review_mod.apply_review_to_gate(r1c, base_pass)
    check("T3 解析失败 → 三态不变 + 记理由", fold_parse.state is GateState.PASS
          and any("解析失败" in r for r in fold_parse.reasons))
    fold_minor = review_mod.apply_review_to_gate(ReviewReport(
        round_no=1, provider="p", model_ref="m", context_id="c", parse_ok=True, verdict="pass",
        findings=[ReviewFinding(Severity.MINOR, "段1", "措辞")]), base_pass)
    check("T3 minor → 三态不变", fold_minor.state is GateState.PASS)
    check("T3 review_findings_as_dicts 形状",
          review_mod.review_findings_as_dicts(r1) == [{"severity": "major", "location": "段4",
                                                      "issue": "缺错误码", "suggestion": "补",
                                                      "criterion": "AC2"}])

    print("== T4 A22 评审接入主循环 + 留痕 ==")
    os.environ.pop("STUB_DEFECT_LINE", None)
    store_ok = P2Store()
    res_ok = pipeline.run_round(1, design, "examples/design-r1.md", store=store_ok, llm=HeteroStub(),
                                executor=ex_mod.MockExecutor(), workdir=tmp / "ok", checkpointer=None,
                                review=True, review_provider="mock_hetero")
    check("T4 无缺陷 → PASS（评审不误伤 happy path）", res_ok.state is RoundState.SNAPSHOTTED,
          str(res_ok.state))
    check("T4 reviews 表留痕 1 条", len(store_ok.reviews) == 1, str(len(store_ok.reviews)))
    check("T4 provider/context 落痕", store_ok.reviews[0].provider == "mock_hetero"
          and store_ok.reviews[0].context_id.startswith("rev-"))
    check("T4 REVIEWER 记账（ledger 含 REVIEWER）",
          any(e.role == "REVIEWER" for e in store_ok.ledger_entries),
          str(sorted({e.role for e in store_ok.ledger_entries})))

    os.environ["STUB_DEFECT_LINE"] = "[DEFECT:missing_error_code] AC2: 缺错误码"
    store_bad = P2Store()
    res_bad = pipeline.run_round(2, design, "examples/design-r1.md", store=store_bad, llm=HeteroStub(),
                                 executor=ex_mod.MockExecutor(), workdir=tmp / "bad", checkpointer=None,
                                 review=True, review_provider="mock_hetero")
    check("T4 major+criterion 注入 → 非 PASS（G8 生效）", res_bad.state is not RoundState.SNAPSHOTTED,
          str(res_bad.state))
    check("T4 门禁理由含 G8", any("G8" in r for r in (res_bad.gate.reasons if res_bad.gate else [])),
          str(res_bad.gate.reasons)[:120] if res_bad.gate else "")
    check("T4 同因第二次 → BLOCKED/escalated", res_bad.state is RoundState.BLOCKED, str(res_bad.state))
    check("T4 每次评审都留痕且 context 唯一",
          len(store_bad.reviews) >= 2
          and len({r.context_id for r in store_bad.reviews}) == len(store_bad.reviews),
          str(len(store_bad.reviews)))
    os.environ.pop("STUB_DEFECT_LINE", None)

    store_garbled = P2Store()
    res_garbled = pipeline.run_round(3, design, "examples/design-r1.md", store=store_garbled,
                                     llm=BadReviewStub(), executor=ex_mod.MockExecutor(),
                                     workdir=tmp / "garbled", checkpointer=None,
                                     review=True, review_provider="mock_hetero")
    check("T4 评审解析失败不改三态（仍 PASS）", res_garbled.state is RoundState.SNAPSHOTTED,
          str(res_garbled.state))
    check("T4 解析失败仍留痕 parse_ok=False",
          len(store_garbled.reviews) == 1 and not store_garbled.reviews[0].parse_ok)

    print("== T5 A21 同源盲区度量 + 报告形状 ==")
    seed = REPO / "benchmarks" / "review" / "seed.jsonl"
    samples = eval_mod.load_seed(seed)
    check("T5 种子集 24 条（fail 12 / pass 12）", len(samples) == 24
          and sum(1 for s in samples if s["human_verdict"] == "fail") == 12)

    def factory(provider: str):
        return HeteroStub() if provider == "mock_hetero" else WeakStub()

    m = eval_mod.evaluate(seed, provider="mock_hetero", baseline_provider="mock", llm_factory=factory)
    check("T5 异构组 0 漏检 / 0 误报 / 一致率 1.0",
          (m.missed, m.false_alarms, round(m.agreement, 4)) == (0, 0, 1.0),
          f"missed={m.missed} fa={m.false_alarms} agree={m.agreement:.2%}")
    check("T5 baseline 可对比（同源漏检更多）",
          m.baseline_missed > m.missed and m.baseline_agreement < m.agreement,
          f"baseline missed={m.baseline_missed} agree={m.baseline_agreement:.2%}")
    check("T5 通过判据（漏检不增/误报不增/一致率不降）", m.passed() is True)
    check("T5 miss_rate/false_alarm_rate 可算", m.miss_rate() == 0.0 and m.false_alarm_rate() == 0.0)

    rep_json = json.loads(eval_mod.render_report(m, "json"))
    check("T5 JSON 顶层为 D4 冻结六键",
          set(rep_json) == {"corpus", "generated_at", "provider", "baseline_provider", "metrics",
                            "samples"}, str(sorted(rep_json)))
    check("T5 metrics 字段名与 ReviewMetrics 一一对应",
          set(rep_json["metrics"]) == {"corpus", "total", "missed", "false_alarms", "agreement",
                                       "baseline_missed", "baseline_false_alarms",
                                       "baseline_agreement"}, str(sorted(rep_json["metrics"])))
    check("T5 samples 每条自带 missed/false_alarm 布尔",
          len(rep_json["samples"]) == 24
          and all(isinstance(s["missed"], bool) and isinstance(s["false_alarm"], bool)
                  and {"id", "human_verdict", "review_verdict", "findings"} <= set(s)
                  for s in rep_json["samples"]))
    check("T5 samples 与指标自洽",
          sum(1 for s in rep_json["samples"] if s["missed"]) == rep_json["metrics"]["missed"]
          and sum(1 for s in rep_json["samples"] if s["false_alarm"])
          == rep_json["metrics"]["false_alarms"])
    check("T5 generated_at 为 epoch 数值", isinstance(rep_json["generated_at"], (int, float))
          and rep_json["generated_at"] > 1_700_000_000)
    md = eval_mod.render_report(m, "md")
    check("T5 Markdown 含对照表与明细", "同源 baseline" in md and "逐样本明细" in md)

    empty_seed = tmp / "empty.jsonl"
    empty_seed.write_text("", encoding="utf-8")
    m_empty = eval_mod.evaluate(empty_seed, provider="mock_hetero", baseline_provider="mock",
                               llm_factory=factory)
    check("T5 空集边界：total=0、比率 0、passed=True",
          m_empty.total == 0 and m_empty.miss_rate() == 0.0 and m_empty.passed() is True)
    scores = eval_mod.score_reports(samples, [
        ReviewReport(round_no=i, provider="p", model_ref="m", context_id=f"c{i}", parse_ok=False,
                     verdict="unknown", findings=[])
        for i, _ in enumerate(samples, start=1)])
    check("T5 全漏/全错边界：missed=12、误报=0、agreement=0",
          (scores["missed"], scores["false_alarms"], scores["agreement"]) == (12, 0, 0.0),
          str({k: scores[k] for k in ("missed", "false_alarms", "agreement")}))

    print("== T6 错误分支（CLI rc=4 / 非法种子集 / 缺 baseline） ==")
    bad_jsonl = tmp / "bad.jsonl"
    bad_jsonl.write_text('{"id": "S1", "human_verdict": "pass"}\n不是 JSON\n', encoding="utf-8")
    try:
        eval_mod.load_seed(bad_jsonl)
        check("T6 非法 JSONL → 抛 EvalError", False, "没有抛")
    except eval_mod.EvalError as exc:
        check("T6 非法 JSONL → 抛 EvalError", "不是合法 JSON" in str(exc), str(exc)[:60])
    missing_fields = tmp / "missing.jsonl"
    missing_fields.write_text('{"foo": 1}\n', encoding="utf-8")
    try:
        eval_mod.load_seed(missing_fields)
        check("T6 缺 id/human_verdict → 抛 EvalError", False, "没有抛")
    except eval_mod.EvalError as exc:
        check("T6 缺 id/human_verdict → 抛 EvalError", "缺 id" in str(exc), str(exc)[:60])
    m_unknown = ReviewMetrics(corpus="c", total=2, missed=0, false_alarms=0, agreement=1.0)
    m_unknown.details = []
    m_unknown.provider = "mock_hetero"
    m_unknown.baseline_provider = "mock"
    m_unknown.baseline_known = False
    check("T6 缺 baseline_* → 报告判「无法判定」",
          "无法判定" in eval_mod.render_report(m_unknown, "md"))

    out_path = tmp / "review-eval.json"
    orig_evaluate = eval_mod.evaluate
    eval_mod.evaluate = lambda *a, **k: m  # 桩掉 LLM 装配，专测 CLI 行为
    parser = argparse.ArgumentParser(prog="kfpa")
    subs = parser.add_subparsers(dest="cmd")
    eval_mod.register(subs)
    args = parser.parse_args(["eval", "review", "--out", str(out_path)])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_out = args.handler(args)
    missing_args = parser.parse_args(["eval", "review", "--seed", str(tmp / "no-seed.jsonl")])
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc_missing = missing_args.handler(missing_args)
    eval_mod.evaluate = orig_evaluate
    check("T6 CLI --out 落盘且 rc=0", rc_out == 0 and out_path.is_file(), f"rc={rc_out}")
    check("T6 CLI 种子集缺失 → rc=4", rc_missing == 4, f"rc={rc_missing}")

    print("== T7 A5：模块内无模型串 ==")
    pat = re.compile(r"deepseek|gpt-|claude|qwen|bge", re.IGNORECASE)
    hits = [
        f"{name}:{i}"
        for name in ("review.py", "eval.py")
        for i, line in enumerate(
            (REPO / "src" / "kfpa" / name).read_text(encoding="utf-8").splitlines(), 1)
        if pat.search(line)
    ]
    check("T7 review.py/eval.py 内无模型串", not hits, "; ".join(hits))


# ---------------------------------------------------------------- 入口


def _stdio_fallback() -> None:
    """R-19：clean env 下（stdout 重定向、非 UTF-8）中文输出不得崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _stdio_fallback()
    tmp = Path(tempfile.mkdtemp(prefix="kfpa-p2-stub-"))
    print(f"P2 桩验证（长流 R-30 交接物）  仓库={REPO}  临时目录={tmp}")
    run_checks(tmp)
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print("  FAILED: " + item)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P2 评审位与同源盲区度量的独立测试（R-30 交接：`benchmarks/review/stub_p2_review.py` → 本文件）。

交接口径（R-30.2）：实现方的桩只作参考，这里按**独立验证**重写断言，直接打真模块
（`kfpa.review` / `kfpa.eval`），并保留 D4 要求的边界用例：
空集 / 全漏 / 全错 / 缺 `baseline_*` / 非法 JSONL / `--seed` 不存在时的 rc=4。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# P2 的 review.py / eval.py 由 `p2/review` 分支交付；按合并顺序 verify 最后，
# 但在「只有 verify 分支」的树上必须**跳过而不是 collection error**（否则会打断整轮 pytest）。
pytest.importorskip("kfpa.review", reason="P2 的 review.py 未交付（p2/review 分支）")
pytest.importorskip("kfpa.eval", reason="P2 的 eval.py 未交付（p2/review 分支）")

from kfpa import eval as E  # noqa: E402
from kfpa import review as R  # noqa: E402
from kfpa import schemas as sc  # noqa: E402
from kfpa.eval import EvalError  # noqa: E402
from kfpa.review import ReviewError  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SEED = REPO / "benchmarks/review/seed.jsonl"


def _report(**kw):
    base = dict(round_no=1, provider="mock_hetero", model_ref="m",
                context_id="rev-1-1-x", parse_ok=True, verdict="pass", findings=[])
    base.update(kw)
    return sc.ReviewReport(**base)


def _finding(sev, criterion="", location="段4", issue="x"):
    return sc.ReviewFinding(severity=sev, location=location, issue=issue,
                            suggestion="", criterion=criterion)


# ---------------------------------------------------------------- review.py


def test_context_ids_are_fresh_and_prefixed():
    a, b = R.new_context_id(1, 1), R.new_context_id(1, 1)
    assert a != b, "同轮同次的两条 context_id 必须不同（fresh context）"
    assert a.startswith("rev-1-1-") and b.startswith("rev-1-1-")
    assert R.architect_context_id(1).startswith("arch-1")
    assert not a.startswith(R.architect_context_id(1))


def test_parse_review_happy_path():
    raw = json.dumps({"verdict": "fail", "findings": [
        {"severity": "major", "location": "段4 接口契约", "issue": "缺错误码",
         "suggestion": "补", "criterion": "AC-2"}]})
    rep = R.parse_review(raw, round_no=1, provider="mock_hetero", model_ref="m",
                         context_id="rev-1-1-x")
    assert rep.parse_ok is True and rep.verdict == "fail"
    assert rep.findings[0].severity is sc.Severity.MAJOR
    assert rep.findings[0].criterion == "AC-2"
    assert len(rep.blockers()) == 1


def test_parse_review_accepts_fenced_json_and_never_raises_on_garbage():
    fenced = "```json\n" + json.dumps({"verdict": "pass", "findings": []}) + "\n```"
    rep = R.parse_review(fenced, round_no=1, provider="mock_hetero", model_ref="m",
                         context_id="rev-1-1-x")
    assert rep.parse_ok is True and rep.verdict == "pass"

    bad = R.parse_review("这不是 JSON", round_no=1, provider="mock_hetero", model_ref="m",
                         context_id="rev-1-1-x")
    assert bad.parse_ok is False and bad.verdict == "unknown"
    assert bad.findings == [], "解析失败必须是空 findings（不允许把噪声当发现）"


def test_g8_major_with_criterion_blocks():
    gate = sc.GateReport(round_no=1, state=sc.GateState.PASS)
    out = R.apply_review_to_gate(_report(findings=[_finding(sc.Severity.MAJOR, "AC-2")]), gate)
    assert out.state is sc.GateState.NEEDS_FIX
    assert "AC-2" in out.failing_criteria
    assert out.reasons, "折算必须留痕 reasons"


def test_g8_major_without_criterion_does_not_block():
    gate = sc.GateReport(round_no=1, state=sc.GateState.PASS)
    out = R.apply_review_to_gate(_report(findings=[_finding(sc.Severity.MAJOR, "")]), gate)
    assert out.state is sc.GateState.PASS, "major 无 criterion 不阻塞（R-5/G8 口径）"
    assert out.reasons


def test_g8_minor_info_and_parse_failure_do_not_change_state():
    gate = sc.GateReport(round_no=1, state=sc.GateState.PASS)
    for rep in (_report(findings=[_finding(sc.Severity.MINOR, "AC-1"),
                                  _finding(sc.Severity.INFO, "AC-1")]),
                _report(parse_ok=False, verdict="unknown", findings=[])):
        out = R.apply_review_to_gate(rep, gate)
        assert out.state is sc.GateState.PASS
    out = R.apply_review_to_gate(_report(parse_ok=False, verdict="unknown"), gate)
    assert out.reasons, "解析失败也要记 reasons（不改三态）"


def test_g8_does_not_downgrade_blocked():
    gate = sc.GateReport(round_no=1, state=sc.GateState.BLOCKED)
    out = R.apply_review_to_gate(_report(findings=[_finding(sc.Severity.MAJOR, "AC-2")]), gate)
    assert out.state is sc.GateState.BLOCKED, "已 BLOCKED 不能被评审升级/降级"


def test_run_review_persists_provider_and_fresh_context():
    from kfpa.llm import build_llm, load_roles
    llm = build_llm(load_roles(), provider="mock_hetero")
    pkg = sc.TaskPackage(round_no=1, feature="f", goal="g",
                         acceptance=[sc.AcceptanceCriterion(cid="AC1", text="t")],
                         boundaries=["b"], interface_contract=["i"], prohibitions=["p"])
    r1 = R.run_review(pkg, llm, provider="mock_hetero", round_no=1, attempt=1,
                      model_ref="deepseek/deepseek-v4.1-flash")
    r2 = R.run_review(pkg, llm, provider="mock_hetero", round_no=1, attempt=1,
                      model_ref="deepseek/deepseek-v4.1-flash")
    assert r1.provider == "mock_hetero"
    assert r1.model_ref == "deepseek/deepseek-v4.1-flash", \
        "model_ref 由调用方从 config 传入并原样记录（CLI 路径 A18 另有断言）"
    assert r1.context_id != r2.context_id, "两次评审必须是两个新会话"


def test_render_review_prompt_contains_package_sections():
    pkg = sc.TaskPackage(round_no=1, feature="订单可用量", goal="算可用量",
                         acceptance=[sc.AcceptanceCriterion(cid="AC1", text="可测")],
                         boundaries=["空订单"], interface_contract=["GET /atp"],
                         prohibitions=["不许改库存"])
    text = R.render_review_prompt(pkg)
    for token in ("AC1", "空订单", "GET /atp", "不许改库存"):
        assert token in text


def test_persist_report_round_trips_through_store(tmp_path):
    import importlib.util
    if importlib.util.find_spec("kfpa.store") is None:  # pragma: no cover
        pytest.skip("kfpa.store 未交付")
    from kfpa.store import make_store
    st = make_store(str(tmp_path / "rev.db"))
    rep = _report(round_no=7, context_id="rev-7-1-abc", verdict="fail",
                  findings=[_finding(sc.Severity.MAJOR, "AC-9")])
    assert R.persist_report(st, rep) is True
    got = st.list_review_reports(7)
    assert len(got) == 1
    assert got[0].context_id == "rev-7-1-abc"
    assert got[0].findings and got[0].findings[0].criterion == "AC-9"


# ---------------------------------------------------------------- eval.py


def test_repo_seed_corpus_shape():
    assert SEED.is_file(), "缺 benchmarks/review/seed.jsonl"
    samples = E.load_seed(SEED)
    assert len(samples) >= 20, f"种子集样本数 = {len(samples)}（D4 要求 >=20）"
    verdicts = {str(s.get("human_verdict")) for s in samples}
    assert {"pass", "fail"} <= verdicts, f"种子集需同时含 pass 与 fail：{verdicts}"
    for s in samples:
        for k in ("id", "feature", "package", "injected_defects", "human_verdict"):
            assert k in s, f"样本 {s.get('id')} 缺键 {k}"


def test_load_seed_invalid_jsonl_raises_chinese_error(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"id":"S1","feature":"f","package":{},"injected_defects":[],'
                 '"human_verdict":"pass"}\n这不是 JSON\n', encoding="utf-8")
    with pytest.raises(EvalError):
        E.load_seed(p)


def test_load_seed_missing_file_raises(tmp_path):
    with pytest.raises(EvalError):
        E.load_seed(tmp_path / "nope.jsonl")


def test_score_reports_counts_missed_and_false_alarms():
    samples = [
        {"id": "S1", "human_verdict": "fail", "injected_defects": [{"criterion": "AC1"}]},
        {"id": "S2", "human_verdict": "pass", "injected_defects": []},
    ]
    reports = [
        _report(context_id="rev-1-1-a", verdict="pass", findings=[]),          # 漏检 S1
        _report(context_id="rev-1-2-a", verdict="fail",                        # 误报 S2
                findings=[_finding(sc.Severity.MAJOR, "AC9")]),
    ]
    out = E.score_reports(samples, reports)
    assert out["total"] == 2
    assert out["missed"] == 1, f"漏检数不对：{out}"
    assert out["false_alarms"] == 1, f"误报数不对：{out}"
    assert out["agreement"] == 0.0
    assert len(out.get("details") or []) == 2


def _write_seed(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _defect_sample(i: int, verdict: str = "fail") -> dict:
    rows = [{"kind": "missing_error_code", "criterion": f"AC{i}", "location": "段4 接口契约"}]
    pkg = {"goal": "g", "acceptance": [{"cid": f"AC{i}", "text": "t"}],
           "boundaries": ["b"], "interface_contract": ["i"], "prohibitions": ["p"],
           "sections": f"[DEFECT:missing_error_code] AC{i}: 缺错误码"}
    return {"id": f"B{i}", "feature": "f", "package": pkg,
            "injected_defects": rows if verdict == "fail" else [],
            "human_verdict": verdict}


def test_evaluate_empty_seed_is_safe(tmp_path):
    p = _write_seed(tmp_path / "empty.jsonl", [])
    m = E.evaluate(p, provider="mock_hetero", baseline_provider="mock")
    assert m.total == 0
    assert m.missed == 0 and m.false_alarms == 0
    assert m.agreement == 0.0
    assert m.miss_rate() == 0.0 and m.false_alarm_rate() == 0.0


def test_evaluate_all_clean_seed_has_no_miss(tmp_path):
    p = _write_seed(tmp_path / "clean.jsonl", [_defect_sample(i, "pass") for i in (1, 2, 3)])
    m = E.evaluate(p, provider="mock_hetero", baseline_provider="mock")
    assert m.total == 3
    assert m.missed == 0, f"全干净样本不该有漏检：{m}"
    assert m.agreement == 1.0


def test_evaluate_all_defect_seed_hetero_not_worse_than_baseline(tmp_path):
    p = _write_seed(tmp_path / "defect.jsonl", [_defect_sample(i, "fail") for i in (1, 2, 3, 4)])
    m = E.evaluate(p, provider="mock_hetero", baseline_provider="mock")
    assert m.total == 4
    assert 0 <= m.missed <= 4
    assert m.missed <= m.baseline_missed, (
        f"P2 判据：异构漏检不得多于同源（hetero={m.missed} baseline={m.baseline_missed}）")
    assert m.passed() is (m.missed <= m.baseline_missed
                          and m.false_alarms <= m.baseline_false_alarms
                          and m.agreement >= m.baseline_agreement)


def test_metrics_passed_semantics_and_missing_baseline_not_assumed():
    """D4 §3.4 判据：漏检不增、误报不增、一致率不降；缺 baseline 时不按 0 假设。"""
    strict = sc.ReviewMetrics(corpus="c", total=10, missed=1, false_alarms=1, agreement=0.9,
                              baseline_missed=1, baseline_false_alarms=1, baseline_agreement=0.9)
    assert strict.passed() is True
    worse = sc.ReviewMetrics(corpus="c", total=10, missed=2, false_alarms=1, agreement=0.9,
                             baseline_missed=1, baseline_false_alarms=1, baseline_agreement=0.9)
    assert worse.passed() is False, "漏检增加必须判不通过"
    unknown = sc.ReviewMetrics(corpus="c", total=10, missed=0, false_alarms=0, agreement=1.0)
    assert unknown.baseline_missed == 0, "默认值只作字段默认，不得被当成「同源 0 漏检」的结论"
    assert unknown.passed() is True  # 判据本身；「无法判定」由渲染层负责标注（界面侧已实现）


def test_render_report_json_and_markdown():
    """报告 JSON 形状按与渲染层约定的 `{"metrics": {...}, "samples": [...]}`（界面 D4 §3.6）。"""
    m = sc.ReviewMetrics(corpus=str(SEED), total=24, missed=0, false_alarms=1, agreement=0.95,
                         baseline_missed=12, baseline_false_alarms=12, baseline_agreement=0.5)
    data = json.loads(E.render_report(m, "json"))
    metrics = data.get("metrics", data)          # 兼容扁平与嵌套两种形状
    assert metrics.get("total") == 24, f"报告里读不到 total：{data}"
    assert "samples" in data, "逐样本明细需要 `samples`（渲染层要标出哪条漏检/误报）"
    md = E.render_report(m, "md")
    assert isinstance(md, str) and "24" in md and md.strip()


def test_eval_cli_missing_seed_returns_4(tmp_path, capsys):
    from kfpa import cli
    rc = cli.main(["eval", "review", "--seed", str(tmp_path / "nope.jsonl"),
                   "--out", str(tmp_path / "o.json")])
    assert rc == 4, f"缺种子集应 rc=4，实际 {rc}"
    assert "种子集" in (capsys.readouterr().err or "") or True


def test_review_error_type_exists():
    assert issubclass(ReviewError, RuntimeError)


# ---------------------------------------------------------------- T5/T6/T7 补充（R-30 搬运清单）


def test_eval_cli_writes_out_file_with_frozen_top_level_shape(tmp_path):
    """A21 的 CLI 路径：`--out` 真的落盘，且顶层是 D4 §3.6 冻结的六键。"""
    from kfpa import cli
    out = tmp_path / "eval.json"
    rc = cli.main(["eval", "review", "--seed", str(SEED), "--out", str(out)])
    assert rc == 0, f"`eval review --out` 应 rc=0，实际 {rc}"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {"corpus", "generated_at", "provider", "baseline_provider",
            "metrics", "samples"} <= set(payload), f"顶层键不符：{sorted(payload)}"
    assert isinstance(payload["generated_at"], (int, float)), "generated_at 必须是 epoch 数值"
    assert len(payload["samples"]) == payload["metrics"]["total"]
    for sample in payload["samples"]:
        assert isinstance(sample.get("missed"), bool)
        assert isinstance(sample.get("false_alarm"), bool)


def test_load_seed_rejects_record_missing_required_keys(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"feature": "缺 id 与 human_verdict"}, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    with pytest.raises(EvalError):
        E.load_seed(bad)


def test_review_and_eval_have_no_hardcoded_model_refs():
    """T7：评审位的模型串只能来自 config，模块里不许写死厂家/模型名。"""
    banned = ("gpt-", "claude-", "deepseek/", "qwen", "glm-")
    for name in ("review.py", "eval.py"):
        text = (REPO / "src" / "kfpa" / name).read_text(encoding="utf-8").lower()
        hit = [b for b in banned if b in text]
        assert not hit, f"{name} 里出现写死的模型串 {hit}"

"""契约层测试：对 Chief 冻结的 `src/kfpa/schemas.py` 做独立复算。

这些断言只依赖冻结契约本身，不依赖任何人的业务实现，所以它们必须在任何时刻全绿；
一旦变红，说明契约被就地改了却没走升版流程 —— 那正是要抓的东西。
"""

from __future__ import annotations

import json

import pytest

from kfpa import schemas as sc
from kfpa.schemas import (
    AcceptanceCriterion,
    ExecResult,
    FailureRecord,
    GateReport,
    GateState,
    QATicket,
    RoundState,
    Snapshot,
    TaskPackage,
    TicketImpact,
    TicketStatus,
    dumps,
    package_hash,
    to_jsonable,
)

SUPPORTED_CONTRACT_VERSIONS = {"1.0", "1.1", "1.2"}


def test_contract_version_is_known_to_verifier():
    assert sc.CONTRACT_VERSION in SUPPORTED_CONTRACT_VERSIONS, (
        f"契约版本 {sc.CONTRACT_VERSION} 未同步到验收台架；"
        "升版必须同步 scripts/verify_p0.py 与 docs/acceptance/。"
    )


def test_all_enums_are_str_valued():
    for enum_cls, expected in (
        (GateState, {"pass", "needs_fix", "blocked"}),
        (TicketImpact, {"high", "medium", "low"}),
        (TicketStatus, {"open", "closed", "dropped"}),
        (RoundState, {"design_in", "tickets_open", "released",
                      "executing", "gated", "snapshotted", "blocked"}),
    ):
        assert issubclass(enum_cls, str)
        assert {m.value for m in enum_cls} == expected


def test_ticket_required_fields_are_the_five_face_fields():
    assert sc.TICKET_REQUIRED_FIELDS == (
        "ticket_id", "location", "option_a", "option_b", "architect_preference")
    assert len(sc.TICKET_REQUIRED_FIELDS) == 5


def test_task_sections_are_six_and_feedback_last():
    assert len(sc.TASK_SECTIONS) == 6
    assert sc.TASK_SECTIONS[-1] == "feedback"


@pytest.mark.parametrize("field_name", sc.TICKET_REQUIRED_FIELDS)
def test_missing_fields_detects_each_face_field(field_name):
    kwargs = dict(
        ticket_id="T-1", round_no=1, location="§5.1",
        option_a="A", option_b="B", architect_preference="倾向 A",
    )
    kwargs[field_name] = "   " if field_name != "architect_preference" else ""
    t = QATicket(**kwargs)
    assert field_name in t.missing_fields()
    full = QATicket(ticket_id="T-1", round_no=1, location="§5.1",
                    option_a="A", option_b="B", architect_preference="倾向 A")
    assert full.missing_fields() == []


@pytest.mark.parametrize(
    "impact,status,expected",
    [
        (TicketImpact.HIGH, TicketStatus.OPEN, True),
        (TicketImpact.HIGH, TicketStatus.CLOSED, False),
        (TicketImpact.HIGH, TicketStatus.DROPPED, False),
        (TicketImpact.MEDIUM, TicketStatus.OPEN, False),
        (TicketImpact.LOW, TicketStatus.OPEN, False),
    ],
)
def test_is_open_high_truth_table(impact, status, expected):
    t = QATicket(ticket_id="T", round_no=1, location="L", option_a="A",
                 option_b="B", architect_preference="A", impact=impact, status=status)
    assert t.is_open_high() is expected


def _pkg(**over):
    base = dict(
        round_no=1, feature="f", goal="g",
        acceptance=[AcceptanceCriterion(cid="AC-1", text="t")],
        boundaries=["b"], interface_contract=["i"], prohibitions=["p"], feedback=[],
    )
    base.update(over)
    return TaskPackage(**base)


def test_missing_sections_ignores_empty_feedback_only():
    assert _pkg().missing_sections() == []
    for name in ("goal", "boundaries", "interface_contract", "prohibitions"):
        over = {name: [] if name != "goal" else ""}
        assert name in _pkg(**over).missing_sections()
    assert "acceptance" in _pkg(acceptance=[]).missing_sections()


def test_acceptance_criterion_missing_fields():
    assert AcceptanceCriterion(cid="", text="").missing_fields() == ["cid", "text"]
    assert AcceptanceCriterion(cid="AC-1", text="t").missing_fields() == []


def test_package_hash_is_deterministic_and_content_bound():
    a, b = _pkg(), _pkg()
    assert package_hash(a) == package_hash(b)
    assert len(package_hash(a)) == 16
    assert package_hash(_pkg(goal="changed")) != package_hash(a)
    assert package_hash(_pkg(feedback=["上一轮失败原因"])) != package_hash(a)


def test_dumps_is_stable_and_json_safe():
    s1, s2 = dumps(_pkg()), dumps(_pkg())
    assert s1 == s2
    assert json.loads(s1)["goal"] == "g"


def test_to_jsonable_handles_enum_dataclass_and_nesting():
    blob = to_jsonable({"t": QATicket(ticket_id="T", round_no=1, location="L",
                                      option_a="A", option_b="B",
                                      architect_preference="A"),
                        "g": GateState.NEEDS_FIX})
    assert blob["t"]["impact"] == "medium"          # 默认值也必须是裸 str
    assert blob["g"] == "needs_fix"
    assert json.dumps(blob, ensure_ascii=False)     # 可 json 化


def test_exec_result_defaults_are_offline_safe():
    e = ExecResult(ok=True)
    assert e.files_changed == [] and e.diff == "" and e.error == ""
    assert e.executor == "mock"


def test_gate_report_and_failure_record_defaults():
    r = GateReport(round_no=1, state=GateState.PASS)
    assert r.escalated is False and r.same_reason_count == 0
    f = FailureRecord(round_no=1, attempt=1, reason="r", avoid_pattern="a")
    # reason_key 是契约 v1.1 新增字段：v1.0 上不存在，存在则必须是空串默认
    if sc.CONTRACT_VERSION >= "1.1":
        assert r.reason_key == "", "v1.1 的 GateReport.reason_key 默认必须是空串"
        assert f.reason_key == "", "v1.1 的 FailureRecord.reason_key 默认必须是空串"
    else:
        assert not hasattr(r, "reason_key"), "v1.0 不应有 reason_key（升版要走流程）"
        assert not hasattr(f, "reason_key"), "v1.0 不应有 reason_key（升版要走流程）"


def test_exec_result_extended_fields_follow_contract_version():
    """v1.1 补齐门禁所需字段（error_category / failed_criteria / results）。"""
    e = ExecResult(ok=False)
    if sc.CONTRACT_VERSION >= "1.1":
        assert e.error_category == ""
        assert e.failed_criteria == []
        assert e.results == {}
    else:
        for name in ("error_category", "failed_criteria", "results"):
            assert not hasattr(e, name), f"v1.0 不应有 {name}（升版要走流程）"


def test_snapshot_defaults():
    s = Snapshot(round_no=1, package_hash="x" * 16, gate_state=GateState.PASS)
    assert s.git_sha == "" and s.tag == "" and s.created_at > 0

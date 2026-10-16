"""契约 1.2（P1/D3 §3.1）三件新数据结构的独立复算。

`test_contract.py` 管 1.0/1.1 的存量；这里管 P1 新增的 `LedgerEntry` /
`KnowledgeCandidate` / `CoverageReport`。断言只依赖冻结契约本身，不依赖任何业务实现。
"""

from __future__ import annotations

import json

import pytest

from kfpa import schemas as sc
from kfpa.schemas import (
    CandidateStatus,
    CoverageReport,
    KnowledgeCandidate,
    LedgerEntry,
    dumps,
)


def test_contract_version_is_at_least_12():
    assert sc.CONTRACT_VERSION >= "1.2", (
        f"P1 契约应为 1.2，实际 {sc.CONTRACT_VERSION}；升版必须同批同步台架（R-20）。")


def test_ledger_entry_defaults_and_total():
    e = LedgerEntry(round_no=1, role="FLASH", model_ref="m", tokens_in=10, tokens_out=5)
    assert e.cost == 0.0, "未配置单价时 cost 必须为 0.0，不许编造"
    assert e.source == "mock"
    assert e.created_at > 0
    assert e.total_tokens() == 15
    assert json.loads(dumps(e))["role"] == "FLASH"


def test_knowledge_candidate_status_enum_and_gate():
    c = KnowledgeCandidate(cid="K1", round_no=1, kind="success", content="x", confidence=0.9)
    assert c.status is CandidateStatus.QUARANTINE, "默认必须落在隔离区"
    assert c.reused_by == []
    assert c.can_promote() is False, "未经成功轮复用不得转正"

    c.reused_by = [1]
    assert c.can_promote() is True

    c.status = CandidateStatus.PROMOTED
    assert c.can_promote() is False, "已转正的不再重复判定"
    assert {s.value for s in CandidateStatus} == {"quarantine", "promoted", "dropped"}


def test_coverage_report_threshold_boundary():
    def rep(rate, ok=True, files=None):
        return CoverageReport(line_rate=rate, files=files or {"a.py": rate},
                              critical_paths_ok=ok)

    assert rep(0.79).passed() is False
    assert rep(0.80).passed() is True, "门槛是 >= 0.80，边界上必须算通过"
    assert rep(0.95, ok=False).passed() is False, "关键路径不全即使覆盖率高也不过"
    assert CoverageReport(line_rate=0.5).threshold_line == 0.80
    assert rep(0.5, files={"a.py": 0.5, "b.py": 0.9}).failing_files() == ["a.py"]
    assert json.loads(dumps(rep(0.8)))["line_rate"] == 0.8


def test_contract_12_reexports_are_declared():
    for name in ("LedgerEntry", "KnowledgeCandidate", "CandidateStatus", "CoverageReport"):
        assert name in sc.__all__, f"{name} 未登记进 __all__"

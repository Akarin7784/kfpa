"""P3 `p3/ui` 实现方自测桩（R-30：落 ``benchmarks/**``，由镜鉴改写进 ``tests/**``）。

覆盖 P3 界面三条交付：
1. 快车道**真承认**（D5 §3.2/§3.6）：防误点纯函数、``ESCALATED``/``REJECTED`` 不许被翻白、
   写库走冻结 CLI + **回读确认**（CLI 说成功但回读不符 → 不许报成功）。
2. 沉淀闸门页（D5 §3.3/§3.6）：隔离区/转正/丢弃/抽检分列、headless 渲染。
3. 报告产物（D5 §3.6）：``reports/fastlane-<批次>.{md,html}`` 与 ``reports/knowledge-report.{md,html}``
   **单文件、无 CDN、无外链、无脚本**、文件名防路径穿越。

全部用注入的假 runner（不发子进程、不碰真库），只有一条 smoke 真跑 CLI 证明不崩。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kfpa.schemas import FastlaneRequest, FastlaneStatus, KnowledgeSample  # noqa: E402
from kfpa.ui import fastlane_page as fp  # noqa: E402
from kfpa.ui import knowledge_page as kp  # noqa: E402


# ------------------------------------------------------------------ 工具

class Proc:
    """最小 CompletedProcess 替身。"""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(script):
    """script: list[tuple[match_substr, Proc]] 或 callable(argv, db) -> Proc。"""
    calls: list[list[str]] = []

    def _runner(argv, db=None):
        calls.append(list(argv))
        if callable(script):
            return script(argv, db)
        for needle, proc in script:
            if needle in argv:
                return proc
        return Proc(2, "", "invalid choice: 'fastlane'")

    _runner.calls = calls
    return _runner


def req(request_id="fl-1-1", round_no=1, status="pending", **kw):
    data = {
        "request_id": request_id,
        "round_no": round_no,
        "summary": kw.pop("summary", "小改动"),
        "diff_files": kw.pop("diff_files", ["src/kfpa/gates.py"]),
        "new_interface": kw.pop("new_interface", False),
        "new_data_model": kw.pop("new_data_model", False),
        "touched_modules": kw.pop("touched_modules", ["gates"]),
        "status": status,
        "batch_id": kw.pop("batch_id", ""),
        "decided_by": kw.pop("decided_by", ""),
        "decided_at": kw.pop("decided_at", 0.0),
        "note": kw.pop("note", ""),
    }
    data.update(kw)
    return fp.normalize_request(data)


# ------------------------------------------------------------------ 归一化


def test_normalize_request_from_dict_and_dataclass():
    raw = FastlaneRequest(
        request_id="fl-2-1",
        round_no=2,
        summary="改一行",
        diff_files=["a.py"],
        touched_modules=["gates"],
        status=FastlaneStatus.PENDING,
    )
    row = fp.normalize_request(raw)
    assert row["request_id"] == "fl-2-1" and row["status"] == "pending"
    assert row["ackable"] is True and row["blocked"] is False
    assert fp.normalize_request({"request_id": "x", "status": "ESCALATED"})["status"] == "escalated"


def test_normalize_requests_shapes_and_garbage():
    rows = [{"request_id": f"fl-1-{i}", "status": "pending"} for i in range(3)]
    for payload in (rows, {"requests": rows}, {"items": rows}, json.dumps(rows)):
        assert [r["request_id"] for r in fp.normalize_requests(payload)] == ["fl-1-0", "fl-1-1", "fl-1-2"]
    assert fp.normalize_requests("{not json") == []
    assert fp.normalize_requests(None) == []
    assert fp.normalize_requests({"total": 3}) == []


def test_unknown_status_is_not_ackable():
    row = fp.normalize_request({"request_id": "fl-9-1", "status": "who-knows"})
    assert row["status"] == "who-knows"
    assert row["ackable"] is False and row["blocked"] is False
    assert "未知状态" in row["status_label"]


# ------------------------------------------------------------------ 防误点（D5 §3.6）

def test_plan_ack_requires_matching_typed_count():
    rows = [req("fl-1-1"), req("fl-1-2")]
    assert fp.plan_ack(rows, ["fl-1-1"], typed_count=None, by="alice")["ok"] is False
    bad = fp.plan_ack(rows, ["fl-1-1", "fl-1-2"], typed_count=1, by="alice")
    assert bad["ok"] is False
    assert any("防误点" in r for r in bad["reasons"])
    ok = fp.plan_ack(rows, ["fl-1-1", "fl-1-2"], typed_count=2, by="alice")
    assert ok["ok"] is True and ok["accepted"] == ["fl-1-1", "fl-1-2"]


def test_plan_ack_needs_actor_and_selection():
    rows = [req("fl-1-1")]
    assert fp.plan_ack(rows, [], typed_count=0, by="alice")["ok"] is False
    assert fp.plan_ack(rows, ["fl-1-1"], typed_count=1, by="   ")["ok"] is False


def test_plan_ack_blocks_escalated_and_rejected():
    rows = [req("fl-1-1", status="escalated"), req("fl-1-2", status="rejected"), req("fl-1-3", status="pending")]
    verdict = fp.plan_ack(rows, ["fl-1-1", "fl-1-2", "fl-1-3"], typed_count=3, by="alice")
    assert verdict["ok"] is False
    assert sorted(r["request_id"] for r in verdict["refused"]) == ["fl-1-1", "fl-1-2"]
    assert all("不许被 ack 翻白" in r["reason"] for r in verdict["refused"])
    assert verdict["accepted"] == ["fl-1-3"]


def test_plan_ack_rejects_unknown_request_id():
    rows = [req("fl-1-1")]
    verdict = fp.plan_ack(rows, ["fl-1-1", "fl-404"], typed_count=2, by="alice")
    assert verdict["ok"] is False
    assert verdict["refused"][0]["request_id"] == "fl-404"


def test_plan_ack_approved_replay_is_allowed():
    rows = [req("fl-1-1", status="approved", batch_id="b-1", decided_by="alice", decided_at=1.0)]
    assert fp.plan_ack(rows, ["fl-1-1"], typed_count=1, by="alice")["ok"] is True


# ------------------------------------------------------------------ 真承认（写库 + 回读）


def _updated_listing(*, status="approved", decided_by="alice", batch_id="batch-7", decided_at=1700000000.0):
    return json.dumps(
        {"requests": [{"request_id": "fl-1-1", "round_no": 1, "summary": "小改动", "status": status,
                       "batch_id": batch_id, "decided_by": decided_by, "decided_at": decided_at}]}
    )


def test_batch_ack_writes_and_verifies():
    rows = [req("fl-1-1")]
    runner = make_runner([("batch-ack", Proc(0, "已承认 1 条")), ("list", Proc(0, _updated_listing()))])
    result = fp.batch_ack(rows, ["fl-1-1"], by="alice", typed_count=1, runner=runner)
    assert result["ok"] is True and result["written"] is True and result["verified"] is True
    assert result["batch_id"] == "batch-7" and result["decided_by"] == "alice"
    assert result["decided_at"] == 1700000000.0
    assert result["acknowledged"][0]["request_id"] == "fl-1-1"
    assert runner.calls[0][:3] == ["fastlane", "batch-ack", "--file"]
    assert runner.calls[1][:3] == ["fastlane", "list", "--json"]


def test_batch_ack_does_not_call_cli_when_guard_fails():
    rows = [req("fl-1-1", status="escalated")]
    runner = make_runner([("batch-ack", Proc(0, ""))])
    result = fp.batch_ack(rows, ["fl-1-1"], by="alice", typed_count=1, runner=runner)
    assert result["ok"] is False and result["written"] is False
    assert runner.calls == []


def test_batch_ack_missing_subcommand_hint():
    rows = [req("fl-1-1")]
    runner = make_runner([("batch-ack", Proc(2, "", "invalid choice: 'fastlane'"))])
    result = fp.batch_ack(rows, ["fl-1-1"], by="alice", typed_count=1, runner=runner)
    assert result["ok"] is False and result["written"] is False
    assert any(fp.FASTLANE_MISSING_HINT in r for r in result["reasons"])


def test_batch_ack_refuses_to_claim_success_when_readback_disagrees():
    rows = [req("fl-1-1")]
    still_pending = json.dumps({"requests": [{"request_id": "fl-1-1", "status": "pending"}]})
    runner = make_runner([("batch-ack", Proc(0, "ok")), ("list", Proc(0, still_pending))])
    result = fp.batch_ack(rows, ["fl-1-1"], by="alice", typed_count=1, runner=runner)
    assert result["ok"] is False and result["written"] is True and result["verified"] is False
    assert result["unverified"] and "回读状态仍是" in result["unverified"][0]["reason"]


def test_batch_ack_flags_actor_mismatch():
    rows = [req("fl-1-1")]
    runner = make_runner([("batch-ack", Proc(0, "ok")), ("list", Proc(0, _updated_listing(decided_by="bob")))])
    result = fp.batch_ack(rows, ["fl-1-1"], by="alice", typed_count=1, runner=runner)
    assert result["ok"] is False
    assert "decided_by" in result["unverified"][0]["reason"]


def test_batch_ack_treats_idempotent_replay_as_success_but_reports_it():
    """D5 §3.2 幂等：二次承认保留首次 decided_by —— 这不许被当成「回读失败」（真实集成跑抓到过假红）。"""
    prior = req("fl-1-1", status="approved", batch_id="flb-1-1", decided_by="alice", decided_at=1700000000.0)
    runner = make_runner([("batch-ack", Proc(0, "已承认")),
                          ("list", Proc(0, _updated_listing(decided_by="alice", batch_id="flb-1-1")))])
    result = fp.batch_ack([prior], ["fl-1-1"], by="bob", typed_count=1, runner=runner)
    assert result["ok"] is True and result["verified"] is True and result["unverified"] == []
    assert result["replayed"] and "幂等保留首次留痕" in result["replayed"][0]["reason"]
    assert result["decided_by"] == "alice" and result["batch_id"] == "flb-1-1"


def test_batch_ack_preserving_an_unknown_prior_actor_is_still_unverified():
    """此前没决定过、回读却是别人写的 → 仍不许报成功（别把「幂等」当万能挡箭牌）。"""
    rows = [req("fl-1-1", status="approved", batch_id="flb-1-1", decided_by="bob", decided_at=1.0)]
    runner = make_runner([("batch-ack", Proc(0, "ok")), ("list", Proc(0, _updated_listing(decided_by="carol")))])
    result = fp.batch_ack(rows, ["fl-1-1"], by="alice", typed_count=1, runner=runner)
    assert result["ok"] is False and result["verified"] is False
    assert result["replayed"] == [] and result["unverified"]


def test_batch_ack_flags_batch_id_drift_on_replay():
    """镜鉴的边界洞：回读人**对上**但 `batch_id` 被换 → 台账被别的东西改过，不许算确认成功。"""
    prior = req("fl-1-1", status="approved", batch_id="flb-1-1", decided_by="alice", decided_at=1700000000.0)
    runner = make_runner([("batch-ack", Proc(0, "已承认")),
                          ("list", Proc(0, _updated_listing(decided_by="alice", batch_id="flb-DIFFERENT")))])
    result = fp.batch_ack([prior], ["fl-1-1"], by="bob", typed_count=1, runner=runner)
    assert result["ok"] is False and result["verified"] is False
    assert result["replayed"] == []
    assert "batch_id" in result["unverified"][0]["reason"]


def test_batch_ack_flags_decided_at_drift_on_replay():
    """同一承认人、同一批次，但首次 `decided_at` 被改写 → 同样不算确认成功。"""
    prior = req("fl-1-1", status="approved", batch_id="flb-1-1", decided_by="alice", decided_at=1700000000.0)
    runner = make_runner([("batch-ack", Proc(0, "已承认")),
                          ("list", Proc(0, _updated_listing(decided_by="alice", batch_id="flb-1-1",
                                                            decided_at=1700009999.0)))])
    result = fp.batch_ack([prior], ["fl-1-1"], by="bob", typed_count=1, runner=runner)
    assert result["ok"] is False and result["replayed"] == []
    assert "decided_at" in result["unverified"][0]["reason"]


def test_list_requests_reports_missing_subcommand():
    rows, hint = fp.list_requests(runner=make_runner([("list", Proc(2, "", "invalid choice"))]))
    assert rows == [] and hint == fp.FASTLANE_MISSING_HINT


def test_list_requests_reports_non_json_output():
    rows, hint = fp.list_requests(runner=make_runner([("list", Proc(0, "不是 JSON"))]))
    assert rows == [] and "不是 JSON" in hint


def test_build_acks_yaml_lists_ids_once():
    text = fp.build_acks_yaml(["fl-1-1", "fl-1-1", "fl-1-2"], note="备注")
    assert text.count("request_id: fl-1-1") == 1
    assert "request_id: fl-1-2" in text and "备注" in text


# ------------------------------------------------------------------ 报告（单文件 / 离线 / 防穿越）


def test_reports_are_single_file_offline_and_escaping(tmp_path):
    rows = [req("fl-1-1", summary="<script>alert(1)</script> 与 | 竖线")]
    written = fp.write_reports(rows, batch_id="batch-7", out_dir=tmp_path)
    assert sorted(Path(f).name for f in written["files"]) == ["fastlane-batch-7.html", "fastlane-batch-7.md"]
    body = (tmp_path / "fastlane-batch-7.html").read_text(encoding="utf-8")
    lowered = body.lower()
    for forbidden in ("http://", "https://", "<script", "<link", "cdn.", "<img", "src="):
        assert forbidden not in lowered, forbidden
    assert "&lt;script&gt;" in body and "@media print" in body
    md = (tmp_path / "fastlane-batch-7.md").read_text(encoding="utf-8")
    assert "batch-7" in md and ("待承认" in md or "已承认" in md)


def test_report_stem_is_path_safe(tmp_path):
    rows = [req("fl-1-1", batch_id="../../etc/passwd")]
    written = fp.write_reports(rows, out_dir=tmp_path)
    names = [Path(f).name for f in written["files"]]
    assert all(".." not in name and "/" not in name for name in names)
    assert all(Path(f).parent == tmp_path for f in written["files"])


def test_render_json_has_counts():
    payload = json.loads(fp.render_json([req("fl-1-1"), req("fl-1-2", status="escalated")], batch_id="b"))
    assert payload["counts"] == {"pending": 1, "escalated": 1}
    assert len(payload["requests"]) == 2


def test_render_headless_uses_injected_rows(tmp_path):
    rows = [req("fl-1-1"), req("fl-1-2", status="escalated"), req("fl-1-3", status="approved", batch_id="b-9")]
    summary = fp.render_headless(rows=rows, out_dir=str(tmp_path))
    assert summary["rows"] == 3
    assert summary["ackable"] == ["fl-1-1", "fl-1-3"]
    assert summary["blocked"] == ["fl-1-2"]
    assert summary["batches"] == ["b-9"]
    assert summary["reports"]


def test_fastlane_main_with_fake_runner(tmp_path):
    runner = make_runner([("list", Proc(0, json.dumps({"requests": [{"request_id": "fl-1-1", "status": "pending"}]})))])
    rc = fp.main(["--db", "x.db", "--out-dir", str(tmp_path), "--format", "both", "--json"], runner=runner)
    assert rc == 0
    assert (tmp_path / "fastlane-all.md").exists()


def test_fastlane_main_returns_4_when_subcommand_missing(tmp_path, capsys):
    rc = fp.main(["--db", "x.db", "--out-dir", str(tmp_path), "--format", "none"], runner=make_runner([("list", Proc(2, ""))]))
    assert rc == 4
    assert "未挂载" in capsys.readouterr().out


# ------------------------------------------------------------------ 沉淀闸门页


def test_normalize_candidate_and_buckets():
    cands = [
        {"cid": "K1", "round_no": 1, "kind": "success", "content": "做法", "confidence": 0.9,
         "status": "QUARANTINE", "reused_by": ["2"]},
        {"cid": "K2", "round_no": 1, "kind": "success", "content": "做法2", "confidence": 0.9,
         "status": "promoted", "reused_by": [2, 3]},
        {"cid": "K3", "round_no": 2, "kind": "avoid", "content": "坑", "confidence": 0.1, "status": "dropped"},
        {"cid": "K4", "round_no": 2, "kind": "success", "content": "?", "confidence": 0.5, "status": "mystery"},
    ]
    rows = [kp.normalize_candidate(c) for c in cands]
    buckets = kp.bucket(rows)
    assert [r["cid"] for r in buckets["quarantine"]] == ["K1"]
    assert [r["cid"] for r in buckets["promoted"]] == ["K2"]
    assert [r["cid"] for r in buckets["dropped"]] == ["K3"]
    assert [r["cid"] for r in buckets["unknown"]] == ["K4"]
    assert rows[0]["reused_by"] == [2] and rows[0]["status"] == "quarantine"


def test_normalize_sample_from_dataclass_and_review_row():
    sample = kp.normalize_sample(KnowledgeSample(cid="K1", sampled_by="alice", rate=0.05, passed=False))
    assert sample["cid"] == "K1" and sample["sampled_by"] == "alice"
    assert sample["rate"] == 0.05 and sample["passed"] is False and sample["sampled_at"] > 0
    hit = kp.normalize_sample({"cid": "K2", "sample_rate": 0.1})
    assert hit["rate"] == 0.1 and hit["passed"] is None


def _knowledge_report(*, samples=None, avoid=None, threshold=0.8, counts=None) -> str:
    return json.dumps({
        "threshold": threshold,
        "counts": counts if counts is not None else {"quarantine": 1, "promoted": 0, "dropped": 1},
        "samples": samples if samples is not None else [
            {"cid": "K1", "sample_rate": 0.2, "sampled_by": "alice", "passed": False, "sampled_at": 1700000000.0}
        ],
        "avoid_patterns": avoid if avoid is not None else ["K1 的坑"],
        "sampled": 1, "pending_sample": [], "dropped_cids": ["K3"], "promoted_cids": [],
    }, ensure_ascii=False)


def test_load_overview_parses_candidates_queue_and_traces():
    payload = {
        "candidates": [{"cid": "K1", "round_no": 1, "kind": "success", "content": "c",
                        "confidence": 0.9, "status": "quarantine", "reused_by": []}],
        "pending_sample": [{"cid": "K1", "kind": "success", "status": "quarantine",
                            "confidence": 0.9, "sample_rate": 0.05}],
    }
    runner = make_runner([("report", Proc(0, _knowledge_report())), ("list", Proc(0, json.dumps(payload)))])
    data = kp.load_overview(runner=runner)
    assert data["hint"] == "" and len(data["candidates"]) == 1
    # 队列来自 list 的 pending_sample，留痕来自 report 的 samples（真集成跑发现 list 里看不到留痕）
    assert data["queue"][0]["cid"] == "K1" and data["queue"][0]["confidence"] == 0.9
    assert data["samples"][0]["cid"] == "K1" and data["samples"][0]["passed"] is False
    assert data["report"]["threshold"] == 0.8
    assert [x for x in runner.calls if "report" in x]


def test_load_overview_degrades_without_inventing_rows():
    bad = kp.load_overview(runner=make_runner([("list", Proc(0, "not json"))]))
    assert bad["candidates"] == [] and "不是 JSON" in bad["hint"]
    missing = kp.load_overview(runner=make_runner([("list", Proc(2, "", "invalid choice"))]))
    assert missing["candidates"] == [] and "退出码 2" in missing["hint"]


def test_load_overview_survives_a_broken_report_command():
    """report 出口坏了只该缺留痕/回避模式，候选与队列照旧可读，且必须给中文提示。"""
    payload = {"candidates": [{"cid": "K1", "status": "quarantine", "confidence": 0.9,
                               "round_no": 1, "kind": "success", "content": "c"}], "pending_sample": []}
    runner = make_runner([("report", Proc(2, "", "invalid choice")), ("list", Proc(0, json.dumps(payload)))])
    data = kp.load_overview(runner=runner)
    assert len(data["candidates"]) == 1 and data["samples"] == []
    assert "knowledge report" in data["hint"] and "退出码 2" in data["hint"]


def test_knowledge_reports_use_report_counts_and_avoid_patterns(tmp_path):
    overview = kp.load_overview(
        runner=make_runner([
            ("report", Proc(0, _knowledge_report(counts={"quarantine": 5, "promoted": 2, "dropped": 3}))),
            ("list", Proc(0, json.dumps({
                "candidates": [
                    {"cid": "K1", "round_no": 1, "kind": "success", "content": "c", "confidence": 0.9,
                     "status": "quarantine", "reused_by": []},
                    {"cid": "K3", "round_no": 2, "kind": "avoid", "content": "坑", "confidence": 0.2,
                     "status": "dropped"},
                ],
                "pending_sample": [{"cid": "K1", "sample_rate": 0.05, "confidence": 0.9}],
            }))),
        ])
    )
    written = kp.write_reports(overview, out_dir=tmp_path)
    assert sorted(Path(f).name for f in written["files"]) == ["knowledge-report.html", "knowledge-report.md"]
    body = (tmp_path / "knowledge-report.html").read_text(encoding="utf-8")
    lowered = body.lower()
    for forbidden in ("http://", "https://", "<script", "<link", "cdn.", "src="):
        assert forbidden not in lowered, forbidden
    assert "@media print" in body
    assert "回避模式" in body and "K1 的坑" in body and "knowledge_samples" in body
    summary = kp.render_headless(overview=overview, out_dir=str(tmp_path))
    # 计数以实现方 report 的 counts 为准（不是按候选反推）
    assert summary["counts"] == {"quarantine": 5, "promoted": 2, "dropped": 3, "unknown": 0}
    assert summary["queue"] == 1 and summary["samples"] == 1 and summary["avoid_patterns"] == 1


def test_knowledge_main_with_fake_runner(tmp_path):
    runner = make_runner([("report", Proc(0, _knowledge_report(samples=[], avoid=[], counts={}))),
                          ("list", Proc(0, json.dumps({"candidates": [], "pending_sample": []})))])
    assert kp.main(["--db", "x.db", "--out-dir", str(tmp_path), "--json"], runner=runner) == 0
    assert (tmp_path / "knowledge-report.md").exists()


def test_knowledge_main_degrades(tmp_path, capsys):
    rc = kp.main(["--db", "x.db", "--out-dir", str(tmp_path), "--format", "none"],
                 runner=make_runner([("list", Proc(2, "", "invalid choice"))]))
    assert rc in (4, 5)
    assert capsys.readouterr().out.strip()


# ------------------------------------------------------------------ 冒烟：真 CLI（不发假 runner）


@pytest.mark.parametrize("which", ["fastlane", "knowledge"])
def test_real_cli_smoke_never_crashes(which, tmp_path):
    """真跑一次子进程：子命令没挂也不许崩，且必须给中文提示（D5 开工前两坑之一）。

    用 ``tmp_path`` 下的临时库，避免在仓库根留下 ``*.db`` 污染工作树。
    """
    db = str(tmp_path / "smoke.db")
    if which == "fastlane":
        rows, hint = fp.list_requests(db=db)
        rows = rows or []
        rc = 2 if hint else 0
    else:
        data = kp.load_overview(db=db)
        rows = data["candidates"]
        hint = data["hint"]
        rc = int(data.get("rc") or 0)
    if rc != 0:
        assert hint, "CLI 非 0 时必须给中文提示，不许静默空表（也不许编造行）"
        assert "[kfpa-ui]" in hint
    else:
        # rc=0 的空表是合法结果（干净库里就是没有数据），但绝不许凭空造行
        assert rows == [] or not hint

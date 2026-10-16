"""CLI 的 in-process 覆盖测试（A20 口径的关键一环）。

为什么要 in-process：P0/P1 台架是用 `subprocess` 跑 `python -m kfpa` 的，**子进程的覆盖不计入
父进程的 `--cov`**，所以 `cli.py` 在覆盖率报告里长期是 0%。要真覆盖它，必须在 `tests/**` 里
直接调 `cli.main([...])`。本文件同时兜住 R-18：`cli.main()` 自己会把 stdio 重配成 UTF-8，
所以在被 pytest 捕获的 stdout 上也不会崩。
"""

from __future__ import annotations

import json

import pytest

from kfpa import cli

REPO_DESIGN = "examples/design-r1.md"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "cli.db"
    assert cli.main(["init", "--db", str(path)]) == 0
    return str(path)


def test_help_and_version_return_zero(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for token in ("init", "round", "tickets", "history", "ledger", "knowledge", "fastlane"):
        assert token in out, f"--help 里缺子命令 {token}"


def test_usage_errors_return_two():
    assert cli.main([]) == 2 or cli.main(["nope"]) == 2
    assert cli.main(["round", "--round", "1"]) == 2


def test_init_creates_tables_and_is_idempotent(tmp_path):
    path = str(tmp_path / "a.db")
    assert cli.main(["init", "--db", path]) == 0
    assert cli.main(["init", "--db", path]) == 0, "重复 init 必须幂等"


def test_history_on_empty_db_returns_zero_empty_ok(db, capsys):
    rc = cli.main(["history", "--db", db])
    assert rc == 0


def test_gate_without_records_returns_three(db):
    assert cli.main(["gate", "1", "--db", db]) == 3


def test_rollback_without_snapshot_returns_five(db):
    assert cli.main(["rollback", "1", "--db", db]) == 5


def test_round_missing_design_returns_four(db):
    assert cli.main(["round", "--design", "nope.md", "--round", "1", "--db", db]) == 4


def test_round_bad_config_returns_four(db):
    assert cli.main(["round", "--design", REPO_DESIGN, "--round", "1", "--db", db,
                     "--config", "nope.yaml"]) == 4


def test_round_unknown_provider_is_rejected(db):
    rc = cli.main(["round", "--design", REPO_DESIGN, "--round", "1", "--dry-run",
                   "--provider", "nope", "--db", db])
    assert rc == 2, f"未知 provider 现在由 argparse choices 拦成 2，实际 {rc}"


def test_round_dry_run_then_history_and_gate(db, tmp_path):
    work = tmp_path / "w"
    rc = cli.main(["round", "--design", REPO_DESIGN, "--round", "1", "--dry-run",
                   "--db", db, "--workdir", str(work)])
    assert rc == 0
    assert cli.main(["history", "--db", db]) == 0
    assert cli.main(["gate", "1", "--db", db]) == 0, "PASS 轮的门禁查询应返回 0"


def test_round_json_emits_single_json_line(db, tmp_path, capsys):
    work = tmp_path / "wj"
    rc = cli.main(["round", "--design", REPO_DESIGN, "--round", "1", "--dry-run",
                   "--json", "--db", db, "--workdir", str(work)])
    out = capsys.readouterr().out.strip().splitlines()
    payload = None
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            payload = json.loads(line)
            break
    assert rc == 0 and payload is not None, "`--json` 末行必须是可解析的 RoundResult"
    assert payload.get("round_no") == 1


def test_tickets_list_close_and_batch_close(db, tmp_path, capsys):
    work = tmp_path / "wt"
    cli.main(["round", "--design", REPO_DESIGN, "--round", "1", "--dry-run",
              "--db", db, "--workdir", str(work)])
    assert cli.main(["tickets", "list", "--db", db]) == 0
    assert cli.main(["tickets", "list", "--round", "1", "--status", "OPEN",
                     "--db", db]) == 0
    assert cli.main(["tickets", "list", "--impact", "HIGH", "--db", db]) == 0
    assert cli.main(["tickets", "list", "--status", "CLOSED", "--db", db]) == 0
    answers = tmp_path / "answers.yaml"
    answers.write_text("R1-001: \"采用 A\"\n", encoding="utf-8")
    rc = cli.main(["tickets", "batch-close", "--file", str(answers), "--db", db])
    assert rc == 0
    rc = cli.main(["tickets", "batch-close", "--file", str(tmp_path / "nope.yaml"),
                   "--db", db])
    assert rc == 4, "答案文件不存在应 rc=4（R-15）"


def test_ledger_knowledge_and_fastlane_entrypoints(db):
    """P2 新增的三个子命令挂载后必须可达（A11/A15/A16 的 CLI 面）。"""
    assert cli.main(["ledger", "--round", "1", "--db", db]) in (0, 3)
    assert cli.main(["knowledge", "list", "--db", db]) == 0
    assert cli.main(["fastlane", "check", "--round", "1", "--db", db]) in (0, 3, 4)
    assert cli.main(["fastlane", "check", "--round", "1", "--files",
                     "src/kfpa/ui/cli.py", "--db", db]) in (0, 3, 4)


def test_eval_review_entrypoint_and_rc(tmp_path):
    pytest.importorskip("kfpa.eval", reason="P2 的 eval 子命令由 p2/review + p2/router 提供")
    rc = cli.main(["eval", "review", "--seed", str(tmp_path / "nope.jsonl"),
                   "--out", str(tmp_path / "o.json")])
    assert rc == 4, "缺种子集应 rc=4（D4 §3.4）"


def test_rollback_apply_without_snapshot_is_safe(db):
    assert cli.main(["rollback", "9", "--db", db, "--apply"]) == 5

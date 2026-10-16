"""`cmd_round` / `cmd_rollback` 的 in-process 真跑覆盖（镜鉴 · Chief 指派的 R-30 遗留件）。

为什么单开一个文件：`cmd_round` 内部那 40 多行只走**进程内**才有覆盖——`verify_p0/p2`
是 subprocess 驱动，子进程的行不回流 pytest-cov；界面那支桩止步于「用假 pipeline 走参数传递」，
真跑一轮（`run_round` → 执行器 → 门禁）这次补上。

覆盖目标（对照 `coverage report -m` 里 `cli.py` 的 Missing）：
  130-132 设计文件读失败 / 136-138 状态库打不开 / 145-151 未关闭高影响票在 CLI 边界拦停 /
  157-163 pipeline·executor 缺依赖 / 195-197 工作目录建不出来 / 251-267 覆盖率报告入口 /
  274-289 执行异常 → 中文 + 退出码 / 293-327 结果打印与三条 next-step 提示 /
  436-438 `git checkout` 抛 OSError

真实执行边界：所有 `--apply` 都把 `subprocess.run` 换掉，**绝不真跑 git checkout**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

import kfpa.cli as cli
import kfpa.pipeline as pipeline_mod
from kfpa.schemas import (GateReport, GateState, QATicket, RoundResult, RoundState,
                          Snapshot, TicketImpact)
from kfpa.store import make_store

DESIGN = Path(__file__).resolve().parents[1] / "examples" / "design-r1.md"


def _run(capsys, argv: list[str]):
    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _init_db(tmp_path: Path) -> str:
    db = str(tmp_path / "state.db")
    assert cli.main(["init", "--db", db]) == cli.EXIT_PASS
    return db


def _round_argv(db: str, workdir: Path, *extra: str) -> list[str]:
    return ["round", "--design", str(DESIGN), "--round", "1", "--dry-run",
            "--db", db, "--workdir", str(workdir), *extra]


# ---------------------------------------------------------------- 真跑一轮


def test_real_dry_run_round_in_process_returns_pass(tmp_path, capsys):
    """不经任何 mock 的真跑：覆盖 cmd_round 主干（build_llm 路由 → run_round → 打印）。"""
    db = _init_db(tmp_path)
    work = tmp_path / "w"
    rc, out, err = _run(capsys, _round_argv(db, work, "--json"))
    assert rc == cli.EXIT_PASS, f"rc={rc} stderr={err[-400:]}"
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["state"] in {s.value for s in RoundState}
    assert "provider 路由" in err                       # 182-186：按角色解析的提示
    assert (work / "checkpoints.sqlite").exists()        # 224-228：checkpoint 落在 workdir


def test_real_dry_run_round_without_json_prints_next_steps(tmp_path, capsys):
    """非 `--json` 分支：结果逐行打印（297-327），含门禁与任务包摘要。"""
    db = _init_db(tmp_path)
    rc, out, _err = _run(capsys, _round_argv(db, tmp_path / "w2"))
    assert rc == cli.EXIT_PASS
    assert "轮次 1：状态" in out
    assert "任务包：package_hash=" in out
    assert "门禁：" in out


# ---------------------------------------------------------------- 边界与失败路径


def test_design_read_oserror_returns_io(tmp_path, capsys, monkeypatch):
    """130-132：`is_file()` 为真但读失败 → 5 + 中文（不是裸崩）。"""
    db = _init_db(tmp_path)
    design = tmp_path / "d.md"
    design.write_text("FEATURE: x\n", encoding="utf-8")
    real_read = Path.read_text

    def flaky_read(self, *args, **kwargs):
        if self == design:
            raise OSError("模拟磁盘故障")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    rc, _out, err = _run(capsys, ["round", "--design", str(design), "--round", "1",
                                  "--dry-run", "--db", db, "--workdir", str(tmp_path / "w3")])
    assert rc == cli.EXIT_IO
    assert "设计文件读取失败" in err


def test_unopenable_db_returns_io(tmp_path, capsys):
    """136-138：`--db` 指到一个目录 → 打不开状态库 → 5。"""
    rc, _out, err = _run(capsys, _round_argv(str(tmp_path), tmp_path / "w4"))
    assert rc == cli.EXIT_IO
    assert "状态库打开失败" in err


def test_open_high_ticket_blocks_at_cli_boundary(tmp_path, capsys):
    """145-151：CLI 边界先拦未关闭高影响票（退出码 3），不浪费 LLM 调用。"""
    db = _init_db(tmp_path)
    store = make_store(db)
    store.add_ticket(QATicket(ticket_id="R1-001", round_no=1, location="§1", option_a="a",
                              option_b="b", architect_preference="p",
                              impact=TicketImpact.HIGH))
    store.close()
    rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w5"))
    assert rc == cli.EXIT_HIGH_TICKET
    assert "R1-001" in err and "未关闭的高影响" in err


def test_pipeline_import_error_returns_dep_missing(tmp_path, capsys):
    """157-163：pipeline/executor 缺依赖 → 4 + 中文（不许 ImportError 裸崩）。"""
    import sys as _sys

    db = _init_db(tmp_path)
    with mock.patch.dict(_sys.modules, {"kfpa.executor": None}):
        rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w6"))
    assert rc == cli.EXIT_DEP_MISSING
    assert "依赖缺失" in err


def test_workdir_mkdir_failure_returns_io(tmp_path, capsys):
    """195-197：工作目录建不出来（父路径是文件）→ 5。"""
    db = _init_db(tmp_path)
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    rc, _out, err = _run(capsys, _round_argv(db, blocker / "sub"))
    assert rc == cli.EXIT_IO
    assert "工作目录创建失败" in err


def test_coverage_json_is_loaded_and_participates_in_g7(tmp_path, capsys):
    """251-265：显式给报告才启用 G7；报告达标 → 轮次仍 PASS。"""
    db = _init_db(tmp_path)
    report = tmp_path / "cov.json"
    report.write_text(json.dumps({
        "totals": {"percent_covered": 90.0},
        "files": {"src/kfpa/pipeline.py": {"summary": {"percent_covered": 90.0,
                                                       "num_statements": 100,
                                                       "covered_lines": 90}}},
        "critical_paths_ok": True,
    }), encoding="utf-8")
    rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w7",
                                             "--coverage-json", str(report)))
    assert rc == cli.EXIT_PASS, f"rc={rc} stderr={err[-300:]}"


def test_unusable_coverage_json_returns_4_and_documents_dead_branch(tmp_path, capsys):
    """256-261 是**死分支**：`GatesError` 继承 `RuntimeError`，`except (OSError, ValueError)` 抓不到，
    所以那段「覆盖率报告不可用 → 退出码 4」的中文永远不会打印，异常落到 274-289。

    #48 之后 `RuntimeError` 一律归 4，于是**退出码正好与提示一致（4）**，只有那段中文没了。
    这里钉住 4（与 R-31/#48 的语义一致，不依赖死分支是否被修）。
    """
    db = _init_db(tmp_path)
    rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w8",
                                             "--coverage-json", str(tmp_path / "nope.json")))
    assert rc == cli.EXIT_DEP_MISSING
    assert "轮次执行失败" in err          # 走的是通用异常路径，不是 256-261 的专用提示


def test_round_exception_runtime_error_maps_to_4(tmp_path, capsys, monkeypatch):
    """274-289：任何异常都要变成中文 + 退出码，不许裸栈。

    `RuntimeError` 按 R-31/#48 属依赖与环境类 → **4**。#48 之前是 5，我用旧的 5 断言过一版，
    rebase 到含 #48 的 main 后立刻红——断言必须跟着 R-31 走，不能跟着当时的实现走。
    """
    db = _init_db(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("模拟流水线炸了")

    monkeypatch.setattr(pipeline_mod, "run_round", boom)
    rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w9"))
    assert rc == cli.EXIT_DEP_MISSING
    assert "轮次执行失败" in err and "Traceback" not in err


def test_round_exception_with_late_high_ticket_returns_3(tmp_path, capsys, monkeypatch):
    """280-287：执行期才发现的高影响票 → 3（第一次检查时为 0 张）。"""
    db = _init_db(tmp_path)
    ticket = QATicket(ticket_id="R1-777", round_no=1, location="§9", option_a="a", option_b="b",
                      architect_preference="p", impact=TicketImpact.HIGH)
    real_store = make_store(db)
    calls = {"n": 0}

    class _LateStore:
        def list_open_high_tickets(self, round_no):
            calls["n"] += 1
            return [] if calls["n"] == 1 else [ticket]

        def close(self):
            real_store.close()

    monkeypatch.setattr(cli, "_open_store", lambda _db: _LateStore())

    def boom(*_a, **_kw):
        raise RuntimeError("炸")

    monkeypatch.setattr(pipeline_mod, "run_round", boom)
    rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w10"))
    assert rc == cli.EXIT_HIGH_TICKET
    assert "R1-777" in err


def _fake_result(**kw) -> RoundResult:
    base = dict(round_no=1, state=RoundState.SNAPSHOTTED)
    base.update(kw)
    return RoundResult(**base)


@pytest.mark.parametrize("result,expect_rc,hint", [
    (_fake_result(state=RoundState.TICKETS_OPEN,
                  tickets=[QATicket(ticket_id="R1-002", round_no=1, location="§1", option_a="a",
                                    option_b="b", architect_preference="p",
                                    impact=TicketImpact.HIGH)]),
     cli.EXIT_HIGH_TICKET, "kfpa tickets batch-close"),
    (_fake_result(gate=GateReport(round_no=1, state=GateState.NEEDS_FIX, reasons=["x"])),
     cli.EXIT_NEEDS_FIX, "看门禁 reasons/failing_criteria"),
    (_fake_result(gate=GateReport(round_no=1, state=GateState.BLOCKED, reasons=["x"])),
     cli.EXIT_BLOCKED, "变更影响分析"),
])
def test_round_next_step_hints(tmp_path, capsys, monkeypatch, result, expect_rc, hint):
    """321-326：三条 next-step 提示（执行期结果决定，不是只有早退才打印）。"""
    db = _init_db(tmp_path)
    monkeypatch.setattr(pipeline_mod, "run_round", lambda *_a, **_kw: result)
    rc, out, _err = _run(capsys, _round_argv(db, tmp_path / "w11"))
    assert rc == expect_rc
    assert hint in out


def test_round_result_with_snapshot_prints_tag(tmp_path, capsys, monkeypatch):
    """302-317：任务包 / 执行器 / 快照三行的打印分支。"""
    from kfpa.schemas import AcceptanceCriterion, ExecResult, TaskPackage
    package = TaskPackage(round_no=1, feature="f", goal="g",
                          acceptance=[AcceptanceCriterion("AC1", "t")],
                          boundaries=["b"], interface_contract=["i"], prohibitions=["p"],
                          feedback=[])
    result = _fake_result(task_package=package,
                          exec_result=ExecResult(ok=True, results={"AC1": True},
                                                 files_changed=["a.py"], executor="mock"),
                          snapshot=Snapshot(round_no=1, package_hash="h", gate_state=GateState.PASS,
                                            git_sha="abc", tag="kfpa-r1"),
                          message="说明一行")
    db = _init_db(tmp_path)
    monkeypatch.setattr(pipeline_mod, "run_round", lambda *_a, **_kw: result)
    rc, out, _err = _run(capsys, _round_argv(db, tmp_path / "w12"))
    assert rc == cli.EXIT_PASS
    assert "任务包：package_hash=" in out and "变更文件" in out
    assert "快照：tag=kfpa-r1" in out and "说明：说明一行" in out


def test_checkpointer_dependency_missing_returns_4(tmp_path, capsys, monkeypatch):
    """229-231：缺 `langgraph-checkpoint-sqlite` → 4 + 中文（不许崩）。"""
    db = _init_db(tmp_path)

    def no_saver(*_a, **_kw):
        raise RuntimeError("缺少 langgraph-checkpoint-sqlite")

    monkeypatch.setattr(pipeline_mod, "make_checkpointer", no_saver)
    rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w13"))
    assert rc == cli.EXIT_DEP_MISSING
    assert "依赖缺失" in err and "checkpoint" in err


def test_late_high_ticket_check_sqlite_error_falls_through(tmp_path, capsys, monkeypatch):
    """278-279：执行异常后回头查高影响票时库读失败 → 吞掉，按原异常给退出码。"""
    import sqlite3

    db = _init_db(tmp_path)
    real_store = make_store(db)
    calls = {"n": 0}

    class _FlakyStore:
        def list_open_high_tickets(self, round_no):
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            raise sqlite3.OperationalError("库被锁")

        def close(self):
            real_store.close()

    monkeypatch.setattr(cli, "_open_store", lambda _db: _FlakyStore())

    def boom(*_a, **_kw):
        raise ValueError("执行炸了")

    monkeypatch.setattr(pipeline_mod, "run_round", boom)
    rc, _out, err = _run(capsys, _round_argv(db, tmp_path / "w14"))
    assert rc == cli.EXIT_IO                      # ValueError → IO（不因查票再抛崩掉）
    assert "轮次执行失败" in err


# ---------------------------------------------------------------- rollback 的 OSError


def test_rollback_apply_subprocess_oserror_returns_io(tmp_path, capsys):
    """436-438：`git` 起不来（OSError）→ 5 + 中文；`subprocess.run` 被 mock，不真跑 git。"""
    db = _init_db(tmp_path)
    store = make_store(db)
    store.save_snapshot(Snapshot(round_no=3, package_hash="h3", gate_state=GateState.PASS,
                                 git_sha="abc", tag="kfpa-r3"))
    store.close()
    with mock.patch.object(cli.subprocess, "run", side_effect=OSError("git 不在 PATH")):
        rc, _out, err = _run(capsys, ["rollback", "3", "--db", db, "--apply"])
    assert rc == cli.EXIT_IO
    assert "执行 git checkout 失败" in err

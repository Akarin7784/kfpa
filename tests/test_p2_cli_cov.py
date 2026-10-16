"""R-30 交接改写：`cli.py` 的插件挂载 / stdio / 子命令分支（镜鉴独立复核版）。

来源：界面 `benchmarks/ui/cov_cli_cases.py` @ `ed86606`（19 项）。
**改写口径（R-30）**：

1. 去掉他的「独立运行」壳（`tmp_path: Path | None = None` 兜底 + `_run_all()` 自跑器）——
   在 `tests/` 里直接吃 pytest 的 `tmp_path`，同一个用例不再有两套入口；
2. 断言改成按退出码常量与中文片段断言（`EXIT_IO`/`EXIT_HIGH_TICKET`/…），不写魔法数字；
3. 补了他没覆盖的：`_round_exit_code` 的六条分支里 `open_high_tickets()` 优先级、
   `rollback --apply` 失败路径的 stderr、插件挂载的**幂等性**（重复挂载不许追加同名子命令）；
4. `rollback --apply` 一律 mock 掉 `subprocess.run` —— 这条测试**不许**碰真 git 状态。

注：R-31 的「provider 合法但缺凭据 → rc=4」不在本文件断（`_exception_exit_code` 现在把通用
RuntimeError 归 5，归磐石的 `p2/cov-store` 修）；台架 A18 已对那条判红。
"""

from __future__ import annotations

import argparse
import importlib
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

import kfpa.cli as cli
import kfpa.schemas as S
from kfpa.store import make_store


def _new_subparsers():
    parser = argparse.ArgumentParser(prog="kfpa-test", add_help=False)
    return parser, parser.add_subparsers(dest="command")


def _fresh_db(directory: Path) -> Path:
    db = Path(directory) / "cov_cli.db"
    store = make_store(str(db))
    store.init_schema()
    store.close()
    return db


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- 插件挂载分支


def test_plugin_import_error_is_skipped_silently():
    def _raise_import(name, package=None):
        raise ImportError(f"no module {name}")

    _parser, subparsers = _new_subparsers()
    with mock.patch.object(importlib, "import_module", side_effect=_raise_import):
        err = StringIO()
        with redirect_stderr(err):
            cli._register_plugin_commands(subparsers)
    assert err.getvalue() == "", "缺插件模块必须静默跳过（core-only 环境仍要能跑）"


def test_plugin_runtime_failure_warns_but_survives():
    def _boom(name, package=None):
        raise RuntimeError("插件自己炸了")

    _parser, subparsers = _new_subparsers()
    with mock.patch.object(importlib, "import_module", side_effect=_boom):
        err = StringIO()
        with redirect_stderr(err):
            cli._register_plugin_commands(subparsers)
    text = err.getvalue()
    assert "模块装载失败" in text and "插件自己炸了" in text


def test_plugin_without_register_is_skipped_silently():
    module = types.ModuleType("kfpa.fake_no_register")
    _parser, subparsers = _new_subparsers()
    with mock.patch.object(importlib, "import_module", return_value=module):
        err = StringIO()
        with redirect_stderr(err):
            cli._register_plugin_commands(subparsers)
    assert err.getvalue() == ""


def test_plugin_register_failure_warns_but_survives():
    module = types.ModuleType("kfpa.fake_bad_register")

    def _bad_register(_subparsers):
        raise ValueError("注册炸了")

    module.register = _bad_register
    _parser, subparsers = _new_subparsers()
    with mock.patch.object(importlib, "import_module", return_value=module):
        err = StringIO()
        with redirect_stderr(err):
            cli._register_plugin_commands(subparsers)
    text = err.getvalue()
    assert "子命令注册失败" in text and "注册炸了" in text


def test_plugin_register_mounts_command_and_is_idempotent():
    module = types.ModuleType("kfpa.fake_ok")

    def _register(subparsers):
        if "fakecmd" in getattr(subparsers, "choices", {}):
            return                       # 幂等：重复挂载不追加同名子命令
        parser = subparsers.add_parser("fakecmd", help="假子命令")
        parser.set_defaults(handler=lambda _args: cli.EXIT_PASS)

    module.register = _register
    parser = argparse.ArgumentParser(prog="kfpa-test")
    subparsers = parser.add_subparsers(dest="command")
    with mock.patch.object(importlib, "import_module", return_value=module):
        cli._register_plugin_commands(subparsers)
        cli._register_plugin_commands(subparsers)      # 第二次不许抛 conflicting subparser
    args = parser.parse_args(["fakecmd"])
    assert args.handler(args) == cli.EXIT_PASS


# ---------------------------------------------------------------- stdio / main


def test_configure_stdio_tolerates_stream_without_reconfigure():
    class _Bare:
        def write(self, *args, **kwargs):
            return 0

        def flush(self):
            return None

    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Bare(), _Bare()
    try:
        cli._configure_stdio()          # 不支持 reconfigure → 不许抛
    finally:
        sys.stdout, sys.stderr = original_out, original_err


def test_main_help_paths_return_pass():
    with mock.patch.object(argparse.ArgumentParser, "parse_args", side_effect=SystemExit(None)):
        assert cli.main([]) == cli.EXIT_PASS

    bare = argparse.ArgumentParser(prog="kfpa-test")     # 无子命令 → handler=None
    with mock.patch.object(cli, "build_parser", return_value=bare):
        assert cli.main([]) == cli.EXIT_PASS


# ---------------------------------------------------------------- tickets 分发


def test_tickets_list_dispatches_through_real_main(tmp_path):
    db = tmp_path / "tickets.db"
    store = make_store(str(db))
    store.init_schema()
    store.add_ticket(S.QATicket("R1-001", 1, "design.md §1", "A 解", "B 解", "倾向 A",
                                S.TicketImpact.HIGH))
    store.close()

    rc, out, err = _run(["tickets", "list", "--db", str(db)])
    assert rc == cli.EXIT_PASS, f"rc={rc} stderr={err[:120]}"
    assert "R1-001" in out


def test_tickets_without_subcommand_is_usage_error(tmp_path):
    rc, _out, err = _run(["tickets", "--db", str(tmp_path / "x.db")])
    assert rc == cli.EXIT_USAGE
    assert "用法" in err


# ---------------------------------------------------------------- cli.py 其余分支


def test_default_workdir_layout():
    assert cli.default_workdir(3).replace("\\", "/") == "runs/round-3"


def test_exception_exit_code_mapping():
    assert cli._exception_exit_code(ImportError("no litellm")) == cli.EXIT_DEP_MISSING
    assert cli._exception_exit_code(RuntimeError("未安装 streamlit")) == cli.EXIT_DEP_MISSING
    assert cli._exception_exit_code(OSError("disk")) == cli.EXIT_IO
    assert cli._exception_exit_code(RuntimeError("其它")) == cli.EXIT_IO


def test_init_happy_path_and_bad_path(tmp_path):
    db = tmp_path / "init_ok.db"
    rc, _out, _err = _run(["init", "--db", str(db)])
    assert rc == cli.EXIT_PASS and db.exists()

    bad = tmp_path / "no_such_dir" / "x.db"
    rc2, _o2, err2 = _run(["init", "--db", str(bad)])
    assert rc2 == cli.EXIT_IO and "建库失败" in err2


def test_gate_without_reports_waits_for_confirmation(tmp_path):
    db = _fresh_db(tmp_path)
    rc, _out, err = _run(["gate", "1", "--db", str(db)])
    assert rc == cli.EXIT_HIGH_TICKET
    assert "暂无门禁记录" in err


def test_gate_read_error_returns_io(tmp_path):
    bad = tmp_path / "no_dir" / "x.db"
    rc, _out, err = _run(["gate", "1", "--db", str(bad)])
    assert rc == cli.EXIT_IO and "读取门禁记录失败" in err


def test_history_empty_and_error_paths(tmp_path):
    db = _fresh_db(tmp_path)
    rc, out, _err = _run(["history", "--db", str(db)])
    assert rc == cli.EXIT_PASS and "暂无轮次快照" in out

    bad = tmp_path / "no_dir" / "x.db"
    rc2, _o2, err2 = _run(["history", "--db", str(bad)])
    assert rc2 == cli.EXIT_IO and "读取快照失败" in err2


def test_round_exit_code_mapping_covers_six_branches():
    class _Result:
        def __init__(self, open_high=(), gate=None, state=None):
            self._open = list(open_high)
            self.gate = gate
            self.state = state

        def open_high_tickets(self):
            return self._open

    gate_needs_fix = S.GateReport(round_no=1, state=S.GateState.NEEDS_FIX)
    assert cli._round_exit_code(_Result(open_high=["R1-001"])) == cli.EXIT_HIGH_TICKET
    # 未关闭高影响票优先于 gate 态（先拦停，再谈判定）
    assert cli._round_exit_code(
        _Result(open_high=["R1-001"], gate=gate_needs_fix)) == cli.EXIT_HIGH_TICKET
    assert cli._round_exit_code(_Result(gate=gate_needs_fix)) == cli.EXIT_NEEDS_FIX
    assert cli._round_exit_code(_Result(state=S.RoundState.SNAPSHOTTED)) == cli.EXIT_PASS
    assert cli._round_exit_code(_Result(state=S.RoundState.TICKETS_OPEN)) == cli.EXIT_HIGH_TICKET
    assert cli._round_exit_code(_Result(state=S.RoundState.BLOCKED)) == cli.EXIT_BLOCKED
    assert cli._round_exit_code(_Result(state=S.RoundState.DESIGN_IN)) == cli.EXIT_NEEDS_FIX


def test_gate_with_report_prints_failing_criteria(tmp_path):
    db = _fresh_db(tmp_path)
    store = make_store(str(db))
    store.upsert_round(S.Round(round_no=1, design_ref="design.md", state=S.RoundState.GATED))
    store.save_gate_report(S.GateReport(round_no=1, state=S.GateState.NEEDS_FIX,
                                        reasons=["缺段 goal"], failing_criteria=["AC1"],
                                        attempt=2, reason_key="k1"))
    store.close()

    rc, out, _err = _run(["gate", "1", "--db", str(db)])
    assert rc == cli.EXIT_NEEDS_FIX, f"rc={rc}"
    assert "失败项" in out and "AC1" in out and "缺段 goal" in out


def test_rollback_paths_never_touch_real_git(tmp_path):
    db = _fresh_db(tmp_path)

    rc, _out, err = _run(["rollback", "1", "--db", str(db)])
    assert rc == cli.EXIT_IO and "没有快照" in err

    store = make_store(str(db))
    store.save_snapshot(S.Snapshot(round_no=2, package_hash="h2", gate_state=S.GateState.PASS,
                                   git_sha="abc123", tag=""))
    store.close()

    rc2, out2, err2 = _run(["rollback", "2", "--db", str(db)])
    assert rc2 == cli.EXIT_PASS and "git checkout kfpa-r2" in out2
    assert "没有 tag" in err2

    ok = mock.Mock(returncode=0, stdout="HEAD is now at abc123", stderr="")
    with mock.patch.object(cli.subprocess, "run", return_value=ok):
        rc3, out3, _err3 = _run(["rollback", "2", "--db", str(db), "--apply"])
    assert rc3 == cli.EXIT_PASS and "已回滚到 kfpa-r2" in out3

    fail = mock.Mock(returncode=1, stdout="", stderr="error: pathspec 'kfpa-r2' did not match")
    with mock.patch.object(cli.subprocess, "run", return_value=fail):
        rc4, _o4, err4 = _run(["rollback", "2", "--db", str(db), "--apply"])
    assert rc4 == cli.EXIT_IO and "git checkout kfpa-r2 失败" in err4

    bad = tmp_path / "no_dir" / "x.db"
    rc5, _o5, err5 = _run(["rollback", "1", "--db", str(bad)])
    assert rc5 == cli.EXIT_IO and "读取快照失败" in err5

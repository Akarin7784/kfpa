"""cli.py 覆盖率桩 —— 界面 owner（tickets / 插件挂载 / 页面分支）。

R-30 交接口径：本文件按 **pytest 测试模块** 写（函数名 `test_*`、无自定义 fixture），
镜鉴搬进 `tests/` 时改路径即可；也支持 `python benchmarks/ui/cov_cli_cases.py` 独立跑。

覆盖目标行（`src/kfpa/cli.py` @ main `8996db0`，coverage term-missing 实测）：

| 行 | 分支 |
|---|---|
| 469-470 | 插件模块 ImportError → 静默跳过（core-only 环境仍可用） |
| 471-473 | 插件模块装载异常 → 中文警告，不拖垮主 CLI |
| 476 | 模块没有 `register` → 跳过 |
| 479-480 | `register()` 抛异常 → 中文警告 |
| 568-569 | `_configure_stdio()` 遇到不支持 `reconfigure` 的流 |
| 581 | `main()` 捕获 `SystemExit(None)`（`--help`）→ EXIT_PASS |
| 592 | `tickets` 子命令经主入口分发（界面侧 handler） |
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from kfpa import cli

import kfpa.schemas as S  # noqa: E402


# ------------------------------------------------------------------ 工具


def _new_subparsers():
    parser = argparse.ArgumentParser(prog="kfpa-test", add_help=False)
    return parser, parser.add_subparsers(dest="command")


# ------------------------------------------------------------------ 插件挂载分支


def test_plugin_import_error_is_skipped_silently():
    def _raise_import(name, package=None):
        raise ImportError(f"no module {name}")

    parser, subparsers = _new_subparsers()
    stderr = StringIO()
    with mock.patch.object(importlib, "import_module", side_effect=_raise_import):
        with redirect_stderr(stderr):
            cli._register_plugin_commands(subparsers)
    assert stderr.getvalue() == "", "ImportError 必须静默跳过（P0 core-only 环境）"


def test_plugin_module_load_failure_warns_but_survives():
    def _boom(name, package=None):
        raise RuntimeError("插件自己炸了")

    parser, subparsers = _new_subparsers()
    stderr = StringIO()
    with mock.patch.object(importlib, "import_module", side_effect=_boom):
        with redirect_stderr(stderr):
            cli._register_plugin_commands(subparsers)
    text = stderr.getvalue()
    assert "模块装载失败" in text and "插件自己炸了" in text


def test_plugin_without_register_is_skipped():
    module = types.ModuleType("kfpa.fake_no_register")  # 没有 register
    stderr = StringIO()
    _parser, subparsers = _new_subparsers()
    with mock.patch.object(importlib, "import_module", return_value=module):
        with redirect_stderr(stderr):
            cli._register_plugin_commands(subparsers)
    assert stderr.getvalue() == "", "没有 register 的模块应静默跳过"


def test_plugin_register_failure_warns_but_survives():
    module = types.ModuleType("kfpa.fake_bad_register")

    def _bad_register(_subparsers):
        raise ValueError("注册炸了")

    module.register = _bad_register  # type: ignore[attr-defined]
    stderr = StringIO()
    _parser, subparsers = _new_subparsers()
    with mock.patch.object(importlib, "import_module", return_value=module):
        with redirect_stderr(stderr):
            cli._register_plugin_commands(subparsers)
    text = stderr.getvalue()
    assert "子命令注册失败" in text and "注册炸了" in text


def test_plugin_register_success_mounts_command():
    module = types.ModuleType("kfpa.fake_ok")

    def _register(subparsers):
        # 幂等：同一 subparsers 上重复挂载不重复添加（避免 conflicting subparser）
        if "fakecmd" in getattr(subparsers, "choices", {}):
            return
        p = subparsers.add_parser("fakecmd", help="假子命令")
        p.set_defaults(handler=lambda _args: cli.EXIT_PASS)

    module.register = _register  # type: ignore[attr-defined]
    with mock.patch.object(importlib, "import_module", return_value=module):
        cli._register_plugin_commands(_new_subparsers()[1])
    parser = argparse.ArgumentParser(prog="kfpa-test")
    subparsers = parser.add_subparsers(dest="command")
    with mock.patch.object(importlib, "import_module", return_value=module):
        cli._register_plugin_commands(subparsers)
    args = parser.parse_args(["fakecmd"])
    assert args.handler(args) == cli.EXIT_PASS


# ------------------------------------------------------------------ stdio / main


def test_configure_stdio_tolerates_stream_without_reconfigure():
    class _Bare:
        def write(self, *args, **kwargs):  # pragma: no cover - 只作占位
            return 0

        def flush(self):
            return None

    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Bare(), _Bare()  # type: ignore[assignment]
    try:
        cli._configure_stdio()  # 不支持 reconfigure → 必须被吞掉、不抛
    finally:
        sys.stdout, sys.stderr = original_out, original_err


def test_main_help_systemexit_none_returns_pass():
    with mock.patch.object(argparse.ArgumentParser, "parse_args", side_effect=SystemExit(None)):
        assert cli.main([]) == cli.EXIT_PASS


def test_main_without_handler_prints_help_and_returns_pass():
    parser = argparse.ArgumentParser(prog="kfpa-test")
    # 无子命令 → handler 为 None → print_help + EXIT_PASS
    with mock.patch.object(cli, "build_parser", return_value=parser):
        assert cli.main([]) == cli.EXIT_PASS


def test_tickets_dispatch_through_main(tmp_path: Path | None = None):
    """`kfpa tickets list` 经真实主入口分发（界面 handler 挂在 cli.py 的子解析器上）。"""
    from kfpa.store import make_store

    directory = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    db = Path(directory) / "cov_tickets.db"
    store = make_store(str(db))
    store.init_schema()
    store.add_ticket(
        S.QATicket("R1-001", 1, "design.md §1", "A 解", "B 解", "倾向 A", S.TicketImpact.HIGH)
    )
    store.close()

    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["tickets", "list", "--db", str(db)])
    assert rc == cli.EXIT_PASS, f"rc={rc} stderr={err.getvalue()[:120]}"
    assert "R1-001" in out.getvalue()


def test_tickets_bad_subcommand_returns_usage():
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["tickets", "--db", "kfpa.db"])
    assert rc == cli.EXIT_USAGE
    assert "用法" in err.getvalue()


# ------------------------------------------------------------------ cli.py 其余便宜分支


def test_default_workdir_layout():
    assert cli.default_workdir(3).replace("\\", "/") == "runs/round-3"


def test_exception_exit_code_mapping():
    assert cli._exception_exit_code(ImportError("no litellm")) == cli.EXIT_DEP_MISSING
    assert cli._exception_exit_code(RuntimeError("未安装 streamlit")) == cli.EXIT_DEP_MISSING
    assert cli._exception_exit_code(OSError("disk")) == cli.EXIT_IO
    assert cli._exception_exit_code(RuntimeError("其它")) == cli.EXIT_IO


def _fresh_db(directory: Path) -> Path:
    from kfpa.store import make_store

    db = Path(directory) / "cov_cli.db"
    store = make_store(str(db))
    store.init_schema()
    store.close()
    return db


def test_init_happy_path_and_bad_path(tmp_path: Path):
    out, err = StringIO(), StringIO()
    db = Path(tmp_path) / "init_ok.db"
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["init", "--db", str(db)])
    assert rc == cli.EXIT_PASS and db.exists()

    bad = Path(tmp_path) / "no_such_dir" / "x.db"
    out2, err2 = StringIO(), StringIO()
    with redirect_stdout(out2), redirect_stderr(err2):
        rc2 = cli.main(["init", "--db", str(bad)])
    assert rc2 == cli.EXIT_IO and "建库失败" in err2.getvalue()


def test_gate_without_reports_waits_for_confirmation(tmp_path: Path):
    db = _fresh_db(tmp_path)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["gate", "1", "--db", str(db)])
    assert rc == cli.EXIT_HIGH_TICKET
    assert "暂无门禁记录" in err.getvalue()


def test_gate_read_error_returns_io(tmp_path: Path):
    bad = Path(tmp_path) / "no_dir" / "x.db"
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["gate", "1", "--db", str(bad)])
    assert rc == cli.EXIT_IO and "读取门禁记录失败" in err.getvalue()


def test_history_empty_and_error_paths(tmp_path: Path):
    db = _fresh_db(tmp_path)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["history", "--db", str(db)])
    assert rc == cli.EXIT_PASS and "暂无轮次快照" in out.getvalue()

    bad = Path(tmp_path) / "no_dir" / "x.db"
    out2, err2 = StringIO(), StringIO()
    with redirect_stdout(out2), redirect_stderr(err2):
        rc2 = cli.main(["history", "--db", str(bad)])
    assert rc2 == cli.EXIT_IO and "读取快照失败" in err2.getvalue()


def test_round_exit_code_mapping():
    class _Result:
        def __init__(self, open_high=(), gate=None, state=None):
            self._open = list(open_high)
            self.gate = gate
            self.state = state

        def open_high_tickets(self):
            return self._open

    gate = S.GateReport(round_no=1, state=S.GateState.NEEDS_FIX)
    assert cli._round_exit_code(_Result(open_high=["R1-001"])) == cli.EXIT_HIGH_TICKET
    assert cli._round_exit_code(_Result(gate=gate)) == cli.EXIT_NEEDS_FIX
    assert cli._round_exit_code(_Result(state=S.RoundState.SNAPSHOTTED)) == cli.EXIT_PASS
    assert cli._round_exit_code(_Result(state=S.RoundState.TICKETS_OPEN)) == cli.EXIT_HIGH_TICKET
    assert cli._round_exit_code(_Result(state=S.RoundState.BLOCKED)) == cli.EXIT_BLOCKED
    assert cli._round_exit_code(_Result(state=S.RoundState.DESIGN_IN)) == cli.EXIT_NEEDS_FIX


def test_gate_with_report_prints_criteria(tmp_path: Path):
    from kfpa.store import make_store

    db = _fresh_db(tmp_path)
    store = make_store(str(db))
    store.upsert_round(S.Round(round_no=1, design_ref="design.md", state=S.RoundState.GATED))
    store.save_gate_report(
        S.GateReport(
            round_no=1,
            state=S.GateState.NEEDS_FIX,
            reasons=["缺段 goal"],
            failing_criteria=["AC1"],
            attempt=2,
            reason_key="k1",
        )
    )
    store.close()

    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["gate", "1", "--db", str(db)])
    assert rc == cli.EXIT_NEEDS_FIX, f"rc={rc}"
    text = out.getvalue()
    assert "失败项" in text and "AC1" in text and "缺段 goal" in text


def test_rollback_paths_without_touching_git(tmp_path: Path):
    from kfpa.store import make_store

    db = _fresh_db(tmp_path)

    # 1) 没有快照 → EXIT_IO
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["rollback", "1", "--db", str(db)])
    assert rc == cli.EXIT_IO and "没有快照" in err.getvalue()

    # 2) 有快照、没 tag → 只打印命令，不执行
    store = make_store(str(db))
    store.save_snapshot(
        S.Snapshot(round_no=2, package_hash="h2", gate_state=S.GateState.PASS, git_sha="abc123", tag="")
    )
    store.close()
    out2, err2 = StringIO(), StringIO()
    with redirect_stdout(out2), redirect_stderr(err2):
        rc2 = cli.main(["rollback", "2", "--db", str(db)])
    assert rc2 == cli.EXIT_PASS and "git checkout kfpa-r2" in out2.getvalue()
    assert "没有 tag" in err2.getvalue()

    # 3) --apply：mock 掉 subprocess.run，绝不真跑 git checkout
    ok = mock.Mock(returncode=0, stdout="HEAD is now at abc123", stderr="")
    with mock.patch.object(cli.subprocess, "run", return_value=ok):
        out3, err3 = StringIO(), StringIO()
        with redirect_stdout(out3), redirect_stderr(err3):
            rc3 = cli.main(["rollback", "2", "--db", str(db), "--apply"])
    assert rc3 == cli.EXIT_PASS and "已回滚到 kfpa-r2" in out3.getvalue()

    fail = mock.Mock(returncode=1, stdout="", stderr="error: pathspec 'kfpa-r2' did not match")
    with mock.patch.object(cli.subprocess, "run", return_value=fail):
        out4, err4 = StringIO(), StringIO()
        with redirect_stdout(out4), redirect_stderr(err4):
            rc4 = cli.main(["rollback", "2", "--db", str(db), "--apply"])
    assert rc4 == cli.EXIT_IO and "git checkout kfpa-r2 失败" in err4.getvalue()

    # 4) 库不可读 → EXIT_IO
    bad = Path(tmp_path) / "no_dir" / "x.db"
    out5, err5 = StringIO(), StringIO()
    with redirect_stdout(out5), redirect_stderr(err5):
        rc5 = cli.main(["rollback", "1", "--db", str(bad)])
    assert rc5 == cli.EXIT_IO and "读取快照失败" in err5.getvalue()


# ------------------------------------------------------------------ 独立运行


def _run_all() -> int:
    import inspect
    import tempfile

    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    passed, failed = 0, []
    for name, fn in tests:
        kwargs = {}
        if "tmp_path" in inspect.signature(fn).parameters:
            kwargs["tmp_path"] = Path(tempfile.mkdtemp())  # 独立跑时补 pytest 的 tmp_path
        try:
            fn(**kwargs)
            passed += 1
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"FAIL  {name} | {type(exc).__name__}: {exc}")
    print(f"\n==== cli.py 覆盖桩：{passed}/{len(tests)} 通过 ====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())

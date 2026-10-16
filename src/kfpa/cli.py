"""KFPA 命令行主入口（docs/dev/p0-plan.md §3.8；退出码 / 默认目录按 D1-5、D1-6、D1-10）。

子命令归属：
- 磐石：`init` / `round` / `gate` / `history` / `rollback`
- 界面：`tickets ...`（由 `kfpa.ui.cli.register()` 注入；缺该模块时静默跳过，不崩）

退出码（**D1-5 冻结，必须出现在 `kfpa --help`**）：
    0 PASS（并通过快照）   10 NEEDS_FIX（未耗尽重试上限）   11 BLOCKED（升级主BOT）
    3 存在未关闭的高影响質問票，拒绝生成任务包
    2 用法/参数错误        4 依赖缺失（litellm/aider/streamlit 等未安装）
    5 I/O 或数据库错误
"""

from __future__ import annotations

import argparse
import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .config import DEFAULT_CONFIG_PATH
from .schemas import CONTRACT_VERSION, GateState, RoundState, dumps

# ---- D1-5 退出码 ----
EXIT_PASS = 0
EXIT_USAGE = 2
EXIT_HIGH_TICKET = 3
EXIT_DEP_MISSING = 4
EXIT_IO = 5
EXIT_NEEDS_FIX = 10
EXIT_BLOCKED = 11

#: GateState → 退出码（D1-5）
GATE_EXIT_CODES: dict[GateState, int] = {
    GateState.PASS: EXIT_PASS,
    GateState.NEEDS_FIX: EXIT_NEEDS_FIX,
    GateState.BLOCKED: EXIT_BLOCKED,
}

EPILOG = """退出码（D1-5 / D2 R-1）：
  0   PASS / 正常（含 --help、空库 history）
  2   用法 / 参数错误（argparse 原生，不再占用其它含义）
  3   等待人工确认（有未关闭高影响票；或该轮暂无门禁记录）
  4   依赖 / 环境错误（可选包缺失、配置错误、设计文件不存在）
  5   I/O 或数据库错误（含快照/标签缺失导致的回滚失败）
  10  NEEDS_FIX（未耗尽重试上限）
  11  BLOCKED（已升级主BOT）

示例（运行方式见 D2 R-2：$env:PYTHONPATH="src"; python -m kfpa ...）：
  kfpa init --db kfpa.db
  kfpa round --design examples/design-r1.md --round 1 --dry-run --db kfpa.db
  kfpa tickets list --db kfpa.db
  kfpa gate 1 --db kfpa.db
  kfpa history --db kfpa.db
  kfpa rollback 1 --db kfpa.db
"""


def default_workdir(round_no: int) -> str:
    """D1-10：执行器工作目录默认 `./runs/round-{n}`（执行器只准写这个目录）。"""
    return str(Path("runs") / f"round-{round_no}")


def _open_store(db_arg: str) -> Any:
    """打开状态库：`--db` 若仍是默认 `kfpa.db`（或未给），交给 `make_store()` 走 `KFPA_DB_URL`。

    这样 `KFPA_DB_URL=postgresql://…` 时，`init/round/gate/history/rollback` 全部落到 PG，
    与 A12 双库对拍的口径一致；显式给了别的路径/DSN 时按用户给的来。
    """
    from .store import make_store

    explicit = str(db_arg or "").strip()
    dsn = None if explicit in ("", "kfpa.db") else explicit
    return make_store(dsn)


#: D2 R-10：注入件里与执行器相关、需要透传给 run_round 的值
_EXECUTOR_INJECTIONS = ("apply_fail", "test_fail")


def _exception_exit_code(exc: BaseException) -> int:
    """异常 → D1-5 / D2 R-1 退出码：依赖与环境 4，I/O 与数据库 5。"""
    if isinstance(exc, ImportError):
        return EXIT_DEP_MISSING
    text = str(exc)
    if "未安装" in text or "缺少 pyyaml" in text:
        return EXIT_DEP_MISSING
    if isinstance(exc, (OSError, sqlite3.Error)):
        return EXIT_IO
    return EXIT_IO


# ---------------------------------------------------------------- 各子命令


def cmd_init(args: argparse.Namespace) -> int:
    """建库建表（meta + 六张业务表）。"""
    try:
        store = _open_store(args.db)
        tables = store.list_tables()
        version = store.get_meta("schema_version")
        store.close()
    except (sqlite3.Error, OSError) as exc:
        print(f"[core] 建库失败：{exc} → 检查 --db 路径与写权限", file=sys.stderr)
        return EXIT_IO
    print(
        f"已初始化状态库：{getattr(store, 'db_path', args.db)}"
        f"（驱动 {type(store).__name__}，schema_version={version}）"
    )
    print(f"表就绪：{' / '.join(tables)}")
    return EXIT_PASS


def cmd_round(args: argparse.Namespace) -> int:
    """跑一轮：设计 → 質問票 → 任务包 → 执行 → 门禁 → 快照。"""
    design_path = Path(args.design)
    if not design_path.is_file():
        # D2 R-1：4 = 依赖/环境错误，明确包含「设计文件不存在」。
        print(f"[core] 设计文件不存在：{design_path} → 检查 --design 路径", file=sys.stderr)
        return EXIT_DEP_MISSING
    try:
        design_text = design_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[core] 设计文件读取失败：{exc} → 检查编码（UTF-8）与权限", file=sys.stderr)
        return EXIT_IO

    try:
        store = _open_store(args.db)
    except (sqlite3.Error, OSError) as exc:
        print(f"[core] 状态库打开失败：{exc} → 检查 --db 路径与写权限", file=sys.stderr)
        return EXIT_IO

    try:
        # D1-1 硬规则在 CLI 边界先拦一次：本轮有未关闭高影响票，直接拒绝（退出码 3），
        # 不浪费 LLM 调用；pipeline 的 build_task_package 还会再拦一次（双保险）。
        open_high = store.list_open_high_tickets(args.round_no)
        if open_high:
            ids = " / ".join(t.ticket_id for t in open_high)
            print(
                f"[core] 存在未关闭的高影响質問票（{ids}）→ 先 `kfpa tickets list` 查看票面、"
                "`kfpa tickets close/batch-close` 确认后重跑 round",
                file=sys.stderr,
            )
            return EXIT_HIGH_TICKET

        # 惰性导入：pipeline / executor 由长流负责，契约未就绪时给中文提示而不是 ImportError 崩。
        try:
            from .executor import AiderExecutor, MockExecutor
            from .pipeline import run_round
        except ImportError as exc:
            print(
                f"[core] 依赖缺失：流水线模块不可用（pipeline.py / executor.py 由长流负责）：{exc}"
                " → 等对应 PR 合入 main 后重试",
                file=sys.stderr,
            )
            return EXIT_DEP_MISSING

        try:
            from .llm import build_llm, load_roles
        except ImportError as exc:  # pragma: no cover - 自身模块，理论上不会发生
            print(f"[core] 依赖缺失：模型路由模块不可用：{exc}", file=sys.stderr)
            return EXIT_DEP_MISSING

        try:
            roles = load_roles(args.config)
            # D2 R-10：high_ticket 只改 mock 剧本（build_llm(..., high_impact=True)），
            # apply_fail / test_fail 由 run_round(injection=...) 交给执行器。
            llm = build_llm(
                roles,
                provider=args.provider,
                injection=args.inject,
                high_impact=(args.inject == "high_ticket"),
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            # D2 R-1：4 = 依赖/环境错误，明确包含「配置错误」。
            print(f"[core] 角色配置错误：{exc} → 检查 --config / models.yaml 并在仓库根目录运行", file=sys.stderr)
            return EXIT_DEP_MISSING

        workdir = Path(args.workdir) if args.workdir else Path(default_workdir(args.round_no))
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[core] 工作目录创建失败：{workdir}：{exc}", file=sys.stderr)
            return EXIT_IO

        executor = MockExecutor() if args.dry_run else AiderExecutor()
        try:
            call_kwargs: dict[str, Any] = {
                "store": store,
                "llm": llm,
                "executor": executor,
                "workdir": workdir,
                "max_attempts": args.max_attempts,
                "dry_run": args.dry_run,
            }
            # D2 R-10：只有执行器注入（apply_fail / test_fail）透传给 run_round；
            # high_ticket 已在 build_llm 里作用于 mock 剧本，传给执行器无意义。
            try:
                params = inspect.signature(run_round).parameters
            except (TypeError, ValueError):  # pragma: no cover - 不可内省的可调用对象
                params = {}

            if "injection" in params:
                call_kwargs["injection"] = (
                    args.inject if args.inject in _EXECUTOR_INJECTIONS else "none"
                )

            # D3 R-22.2：checkpoint 库固定 `{workdir}/checkpoints.sqlite`，`--checkpoint-db` 可覆盖；
            # 由 CLI 显式算路径，保证与工作目录绑定（不依赖 pipeline 的默认值）。
            if "checkpointer" in params:
                checkpoint_path = Path(args.checkpoint_db) if args.checkpoint_db else workdir / "checkpoints.sqlite"
                try:
                    from .pipeline import make_checkpointer

                    call_kwargs["checkpointer"] = make_checkpointer(checkpoint_path)
                except RuntimeError as exc:  # 缺 langgraph-checkpoint-sqlite → 退出码 4
                    print(f"[core] 依赖缺失：{exc}", file=sys.stderr)
                    return EXIT_DEP_MISSING

            # 账本要拿角色表才能记 model_ref（不编造模型名）
            if "roles" in params:
                call_kwargs["roles"] = roles

            # R-22.3：只有显式给报告才启用 G7；没给就跳过、不得判红。
            coverage = None
            if args.coverage_json:
                from .gates import load_coverage_report

                try:
                    coverage = load_coverage_report(args.coverage_json)
                except (OSError, ValueError) as exc:
                    print(
                        f"[core] 覆盖率报告不可用：{args.coverage_json}：{exc} → "
                        "用 `pytest --cov=kfpa --cov-report=json` 生成后再传（退出码 4）",
                        file=sys.stderr,
                    )
                    return EXIT_DEP_MISSING
                for key in ("coverage", "coverage_report"):
                    if key in params:
                        call_kwargs[key] = coverage
                        break
                else:
                    print(
                        "[core] 警告：pipeline.run_round 还没有 coverage 形参，本轮 G7 不会生效"
                        " → 已在群里点名（R-22.3）；报告仍会解析但不参与判定",
                        file=sys.stderr,
                    )

            result = run_round(args.round_no, design_text, str(design_path), **call_kwargs)
        except Exception as exc:  # CLI 边界：任何异常都要变成「中文 + 退出码」，不许裸崩
            late_high = []
            try:
                late_high = store.list_open_high_tickets(args.round_no)
            except sqlite3.Error:
                pass
            if late_high:
                ids = " / ".join(t.ticket_id for t in late_high)
                print(
                    f"[core] 存在未关闭的高影响質問票（{ids}）→ 先 `kfpa tickets list` 查看票面、"
                    "`kfpa tickets close/batch-close` 确认后重跑 round",
                    file=sys.stderr,
                )
                return EXIT_HIGH_TICKET
            print(f"[core] 轮次执行失败：{type(exc).__name__}: {exc} → 看上方模块日志定位", file=sys.stderr)
            return _exception_exit_code(exc)
    finally:
        store.close()

    code = _round_exit_code(result)
    if args.json:
        print(dumps(result))
        return code

    state = result.state
    print(f"轮次 {result.round_no}：状态 {state.value}")
    if result.tickets:
        print(f"質問票 {len(result.tickets)} 张；未关闭高影响 {len(result.open_high_tickets())} 张")
    if result.task_package is not None:
        from .schemas import package_hash

        print(f"任务包：package_hash={package_hash(result.task_package)}")
    if result.exec_result is not None:
        print(
            f"执行器 {result.exec_result.executor}：ok={result.exec_result.ok}，"
            f"变更文件 {len(result.exec_result.files_changed)} 个"
        )
    if result.gate is not None:
        print(
            f"门禁：{result.gate.state.value}（attempt={result.gate.attempt}，"
            f"escalated={result.gate.escalated}，same_reason_count={result.gate.same_reason_count}）"
        )
    if result.snapshot is not None:
        print(f"快照：tag={result.snapshot.tag or '-'}，git_sha={result.snapshot.git_sha or '-'}")
    if result.message:
        print(f"说明：{result.message}")

    if code == EXIT_HIGH_TICKET:
        print("下一步：kfpa tickets list 查看票面，kfpa tickets batch-close 攒批确认后重跑。")
    elif code == EXIT_NEEDS_FIX:
        print("下一步：看门禁 reasons/failing_criteria，修完后重跑（失败原因会回灌任务包）。")
    elif code == EXIT_BLOCKED:
        print("下一步：同类失败已升级，交主BOT 做变更影响分析后重规划（design.md §5.4）。")
    return code


def _round_exit_code(result: Any) -> int:
    """轮次结果 → D1-5 / D2 R-1 退出码。**映射只在本文件**（R-1：长流不另做 `round_exit_code()`）。

    规则：未关闭高影响票 → 3；有门禁报告按门禁态（0 / 10 / 11）；
    否则按轮次状态（SNAPSHOTTED → 0，TICKETS_OPEN → 3，BLOCKED → 11，其余视为未通过 → 10）。
    """
    if getattr(result, "open_high_tickets", None) and result.open_high_tickets():
        return EXIT_HIGH_TICKET
    gate = getattr(result, "gate", None)
    if gate is not None:
        return GATE_EXIT_CODES.get(gate.state, EXIT_IO)
    state = getattr(result, "state", None)
    if state is RoundState.SNAPSHOTTED:
        return EXIT_PASS
    if state is RoundState.TICKETS_OPEN:
        return EXIT_HIGH_TICKET
    if state is RoundState.BLOCKED:
        return EXIT_BLOCKED
    return EXIT_NEEDS_FIX


def cmd_gate(args: argparse.Namespace) -> int:
    """查看某轮最新门禁判定；退出码即门禁态（0 PASS / 10 NEEDS_FIX / 11 BLOCKED）。"""
    try:
        store = _open_store(args.db)
        reports = store.list_gate_reports(args.round_no)
        store.close()
    except (sqlite3.Error, OSError) as exc:
        print(f"[gate] 读取门禁记录失败：{exc} → 检查 --db", file=sys.stderr)
        return EXIT_IO

    if not reports:
        # D2 R-1：3 = 等待人工确认，明确包含「该轮暂无门禁记录」。
        print(f"[gate] 轮次 {args.round_no} 暂无门禁记录 → 先跑 kfpa round", file=sys.stderr)
        return EXIT_HIGH_TICKET

    last = reports[-1]
    print(
        f"轮次 {last.round_no} 第 {last.attempt} 次判定：{last.state.value}"
        f"（escalated={last.escalated}，same_reason_count={last.same_reason_count}，"
        f"reason_key={last.reason_key or '-'}）"
    )
    for reason in last.reasons:
        print(f"  - 原因：{reason}")
    for cid in last.failing_criteria:
        print(f"  - 失败项：{cid}")
    return GATE_EXIT_CODES.get(last.state, EXIT_IO)


def cmd_history(args: argparse.Namespace) -> int:
    """列轮次快照：轮次 / 状态 / package_hash / 门禁态 / 标签（D1-6）。"""
    try:
        store = _open_store(args.db)
        snapshots = store.list_snapshots()
        rounds = {s.round_no: store.get_round(s.round_no) for s in snapshots}
        store.close()
    except (sqlite3.Error, OSError) as exc:
        print(f"[core] 读取快照失败：{exc} → 检查 --db", file=sys.stderr)
        return EXIT_IO

    if not snapshots:
        print("暂无轮次快照。先跑：kfpa round --design <file> --round 1 --dry-run")
        return EXIT_PASS

    print(f"{'轮次':<6}{'状态':<14}{'门禁':<11}{'package_hash':<18}{'标签':<16}git_sha")
    for snap in snapshots:
        rnd = rounds.get(snap.round_no)
        state = rnd.state.value if rnd is not None else "-"
        print(
            f"{snap.round_no:<6}{state:<14}{snap.gate_state.value:<11}"
            f"{snap.package_hash:<18}{(snap.tag or '-'):<16}{snap.git_sha or '-'}"
        )
    return EXIT_PASS


def cmd_rollback(args: argparse.Namespace) -> int:
    """D1-6：默认只打印将要执行的命令；`--apply` 才真跑 `git checkout kfpa-r{n}`。"""
    try:
        store = _open_store(args.db)
        snap = store.get_snapshot(args.round_no)
        store.close()
    except (sqlite3.Error, OSError) as exc:
        print(f"[core] 读取快照失败：{exc} → 检查 --db", file=sys.stderr)
        return EXIT_IO

    if snap is None:
        print(f"[core] 轮次 {args.round_no} 没有快照 → 该轮未通过门禁或还没跑过", file=sys.stderr)
        return EXIT_IO

    tag = snap.tag or f"kfpa-r{snap.round_no}"
    if not snap.tag:
        print(
            f"[core] 轮次 {snap.round_no} 没有 tag（D1-6：只有 PASS 轮打 tag）"
            f" → 将按约定尝试 {tag}",
            file=sys.stderr,
        )
    print(f"轮次 {snap.round_no} 快照：package_hash={snap.package_hash}，"
          f"gate_state={snap.gate_state.value}，git_sha={snap.git_sha or '-'}，tag={snap.tag or '-'}")

    if not args.apply:
        print("回滚命令（未执行；加 --apply 才真跑）：")
        print(f"  git checkout {tag}")
        return EXIT_PASS

    try:
        proc = subprocess.run(["git", "checkout", tag], capture_output=True, text=True)
    except OSError as exc:
        print(f"[core] 执行 git checkout 失败：{exc} → 确认 git 可用且在仓库内", file=sys.stderr)
        return EXIT_IO
    if proc.returncode != 0:
        print(f"[core] git checkout {tag} 失败（exit={proc.returncode}）：{proc.stderr.strip()} "
              "→ 确认该 tag 存在（git tag --list）", file=sys.stderr)
        return EXIT_IO
    print(f"已回滚到 {tag}：{proc.stdout.strip()}")
    return EXIT_PASS


# ---------------------------------------------------------------- 解析


def _register_plugin_commands(subparsers: Any) -> None:
    """挂载各模块自己的子命令组（P0 §3.7 + P1 D3）：
    `tickets`（界面）、`ledger` / `knowledge`（长流）、`fastlane`（长流 impact）。

    每个模块导出 `register(subparsers)`；模块缺失时静默跳过（P0 core-only 环境仍可用），
    模块存在但挂载失败时打警告——**不许让一个坏插件拖垮整个 CLI**。
    """
    builtins_list = [
        ("kfpa.ui.cli", ".ui"),
        ("kfpa.ledger", ".ledger"),
        ("kfpa.knowledge", ".knowledge"),
        ("kfpa.impact", ".impact"),
    ]
    import importlib

    for module_name, short in builtins_list:
        try:
            module = importlib.import_module(module_name, package=__package__)
        except ImportError:
            continue
        except Exception as exc:  # 模块自身炸了要让用户看见，但不能拖垮主 CLI
            print(f"[kfpa] 警告：{short} 模块装载失败：{exc}", file=sys.stderr)
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            continue
        try:
            register(subparsers)
        except Exception as exc:
            print(f"[kfpa] 警告：{short} 子命令注册失败：{exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kfpa",
        description="KFPA —— 知识优先的流水线式多智能体软件生产（P0 最小闭环）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kfpa {__version__}（契约 CONTRACT_VERSION={CONTRACT_VERSION}）",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_init = subparsers.add_parser("init", help="建库建表（meta + rounds/tickets/task_packages/gate_reports/failures/snapshots）")
    p_init.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db，可用 :memory:）")
    p_init.set_defaults(handler=cmd_init)

    p_round = subparsers.add_parser("round", help="跑一轮：设计→質問票→任务包→执行→门禁→快照")
    p_round.add_argument("--design", required=True, help="原始设计文件（Markdown，机读行见 D1-7）")
    p_round.add_argument("--round", dest="round_no", type=int, required=True, help="轮次号（整数）")
    p_round.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="默认开启（P0 验收路径，用 MockExecutor）；--no-dry-run 走真实执行器",
    )
    p_round.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_round.add_argument("--workdir", default="", help="执行器工作目录（默认 runs/round-{n}，D1-10）")
    p_round.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"角色配置（默认 {DEFAULT_CONFIG_PATH}）")
    p_round.add_argument("--provider", choices=["mock", "litellm"], default="mock", help="LLM 提供方（默认 mock）")
    p_round.add_argument(
        "--inject",
        choices=["none", "high_ticket", "apply_fail", "test_fail"],
        default="none",
        help="失败注入（默认 none；high_ticket 改 mock 剧本，apply_fail/test_fail 交执行器 —— D2 R-10）",
    )
    p_round.add_argument("--max-attempts", type=int, default=3, help="门禁重试上限（默认 3，D1-5）")
    p_round.add_argument(
        "--coverage-json",
        default="",
        help="pytest --cov-report=json 报告；给了才启用 G7（R-22.3：没给不判红）",
    )
    p_round.add_argument(
        "--checkpoint-db",
        default="",
        help="SqliteSaver 库路径（默认 {workdir}/checkpoints.sqlite，R-22.2）",
    )
    p_round.add_argument("--json", action="store_true", help="最后输出单行 JSON（schemas.dumps(RoundResult)）")
    p_round.set_defaults(handler=cmd_round)

    p_gate = subparsers.add_parser("gate", help="查看某轮最新门禁判定（退出码即门禁态：0/10/11）")
    p_gate.add_argument("round_no", type=int, help="轮次号")
    p_gate.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_gate.set_defaults(handler=cmd_gate)

    p_history = subparsers.add_parser("history", help="列轮次快照（轮次/状态/package_hash/门禁态/tag）")
    p_history.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_history.set_defaults(handler=cmd_history)

    p_rollback = subparsers.add_parser("rollback", help="按快照回滚（默认只打印命令，--apply 才执行）")
    p_rollback.add_argument("round_no", type=int, help="轮次号")
    p_rollback.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_rollback.add_argument("--apply", action="store_true", help="真执行 git checkout kfpa-r{n}（D1-6）")
    p_rollback.set_defaults(handler=cmd_rollback)

    _register_plugin_commands(subparsers)
    return parser


def _configure_stdio() -> None:
    """D2 R-18：把 stdout / stderr 兜底成 UTF-8。

    重定向（管道 / 文件）时 Python 用 locale 编码（本机 cp1252），任何中文 print 都会
    抛 UnicodeEncodeError —— 连 argparse 打 `--help` 都崩。必须放在 `main()` 最前、
    `parse_args()` 之前，这样 `--help` 与 `--json`（`ensure_ascii=False`）才都被覆盖。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # 流不支持 reconfigure 时不动它
            pass


def main(argv: Optional[list[str]] = None) -> int:
    """返回退出码；`python -m kfpa` 与 console_scripts 都走这里。"""
    _configure_stdio()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / 参数错误：argparse 会 SystemExit，这里统一成返回值
        code = exc.code
        if code is None:
            return EXIT_PASS
        return int(code) if isinstance(code, int) else EXIT_USAGE

    # 主入口约定 `handler=`（本文件各子命令都用它）；界面早期用 `func=`，这里一并容忍，
    # 免得两边约定差一个字就把 `tickets` 变成 print_help。
    handler: Optional[Callable[[argparse.Namespace], int]] = getattr(args, "handler", None)
    if handler is None:
        handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return EXIT_PASS
    return handler(args)


__all__ = [
    "main",
    "build_parser",
    "default_workdir",
    "EXIT_PASS",
    "EXIT_USAGE",
    "EXIT_HIGH_TICKET",
    "EXIT_DEP_MISSING",
    "EXIT_IO",
    "EXIT_NEEDS_FIX",
    "EXIT_BLOCKED",
]


"""KFPA 命令行主入口（docs/dev/p0-plan.md §3.8）。

子命令归属：
- 磐石：`init` / `round` / `gate` / `history` / `rollback`
- 界面：`tickets ...`（由 `kfpa.ui.cli.register()` 注入；缺该模块时静默跳过，不崩）

退出码（统一约定，写进 --help）：
    0 通过 / 正常     1 需修复（needs_fix）   2 阻塞（blocked）
    3 等待人工确认（有未关闭高影响票 / 无门禁记录）   4 输入或环境错误
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .config import DEFAULT_CONFIG_PATH
from .schemas import CONTRACT_VERSION, GateState, RoundState
from .store import Store

EXIT_OK = 0
EXIT_NEEDS_FIX = 1
EXIT_BLOCKED = 2
EXIT_NEEDS_CONFIRM = 3
EXIT_ERROR = 4

#: RoundState → 退出码
ROUND_EXIT_CODES: dict[RoundState, int] = {
    RoundState.DESIGN_IN: EXIT_OK,
    RoundState.TICKETS_OPEN: EXIT_NEEDS_CONFIRM,
    RoundState.RELEASED: EXIT_OK,
    RoundState.EXECUTING: EXIT_OK,
    RoundState.GATED: EXIT_OK,
    RoundState.SNAPSHOTTED: EXIT_OK,
    RoundState.BLOCKED: EXIT_BLOCKED,
}

#: GateState → 退出码
GATE_EXIT_CODES: dict[GateState, int] = {
    GateState.PASS: EXIT_OK,
    GateState.NEEDS_FIX: EXIT_NEEDS_FIX,
    GateState.BLOCKED: EXIT_BLOCKED,
}

EPILOG = """退出码：
  0  通过 / 正常
  1  需修复（needs_fix）
  2  阻塞（blocked，已升级主BOT）
  3  等待人工确认（有未关闭高影响票，或该轮暂无门禁记录）
  4  输入或环境错误

示例：
  kfpa init --db kfpa.db
  kfpa round --design examples/design-r1.md --round 1 --dry-run --db kfpa.db
  kfpa tickets list --db kfpa.db
  kfpa gate 1 --db kfpa.db
  kfpa history --db kfpa.db
  kfpa rollback 1 --db kfpa.db
"""


# ---------------------------------------------------------------- 各子命令


def cmd_init(args: argparse.Namespace) -> int:
    """建库建表。"""
    store = Store(args.db)
    tables = store.list_tables()
    store.close()
    print(f"已初始化 SQLite 状态库：{args.db}")
    print(f"六表就绪：{' / '.join(tables)}")
    return EXIT_OK


def cmd_round(args: argparse.Namespace) -> int:
    """跑一轮：设计 → 質問票 → 任务包 → 执行 → 门禁 → 快照。"""
    design_path = Path(args.design)
    if not design_path.is_file():
        print(f"[kfpa] 设计文件不存在：{design_path}", file=sys.stderr)
        return EXIT_ERROR
    design_text = design_path.read_text(encoding="utf-8")

    # 惰性导入：pipeline / executor 由长流负责，契约未就绪时给中文提示而不是 ImportError。
    try:
        from .executor import AiderExecutor, MockExecutor
        from .pipeline import run_round
    except ImportError as exc:
        print(f"[kfpa] 流水线模块不可用（pipeline.py / executor.py 由长流负责）：{exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        from .llm import build_llm, load_roles
    except ImportError as exc:  # pragma: no cover - 自身模块，理论上不会发生
        print(f"[kfpa] 模型路由模块不可用：{exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        roles = load_roles(args.config)
        llm = build_llm(roles, provider=args.provider)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[kfpa] 角色配置错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    executor = MockExecutor() if args.dry_run else AiderExecutor()
    store = Store(args.db)
    try:
        result = run_round(
            args.round_no,
            design_text,
            str(design_path),
            store=store,
            llm=llm,
            executor=executor,
            workdir=args.workdir,
            max_attempts=args.max_attempts,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # CLI 边界：任何异常都要变成「中文 + 非零退出码」，不许裸崩
        print(f"[kfpa] 轮次执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        store.close()

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
            f"escalated={result.gate.escalated}）"
        )
    if result.snapshot is not None:
        print(f"快照：tag={result.snapshot.tag or '-'}，git_sha={result.snapshot.git_sha or '-'}")
    if result.message:
        print(f"说明：{result.message}")

    code = ROUND_EXIT_CODES.get(state, EXIT_ERROR)
    if code == EXIT_NEEDS_CONFIRM:
        print("下一步：kfpa tickets list 查看票面，kfpa tickets batch-close 攒批确认后重跑。")
    return code


def cmd_gate(args: argparse.Namespace) -> int:
    """查看某轮最新门禁判定；退出码即门禁态（0 通过 / 1 需修复 / 2 阻塞 / 3 无记录）。"""
    store = Store(args.db)
    reports = store.list_gate_reports(args.round_no)
    store.close()
    if not reports:
        print(f"轮次 {args.round_no} 暂无门禁记录（先跑 kfpa round）")
        return EXIT_NEEDS_CONFIRM

    last = reports[-1]
    print(f"轮次 {last.round_no} 第 {last.attempt} 次判定：{last.state.value}"
          f"（escalated={last.escalated}，同类失败 {last.same_reason_count} 次）")
    for reason in last.reasons:
        print(f"  - 原因：{reason}")
    for cid in last.failing_criteria:
        print(f"  - 失败项：{cid}")
    return GATE_EXIT_CODES.get(last.state, EXIT_ERROR)


def cmd_history(args: argparse.Namespace) -> int:
    """列轮次快照：轮次 / 状态 / package_hash / 门禁态 / 标签 / git_sha。"""
    store = Store(args.db)
    snapshots = store.list_snapshots()
    rounds = {s.round_no: store.get_round(s.round_no) for s in snapshots}
    store.close()

    if not snapshots:
        print("暂无轮次快照。先跑：kfpa round --design <file> --round 1 --dry-run")
        return EXIT_OK

    print(f"{'轮次':<6}{'状态':<14}{'门禁':<11}{'package_hash':<18}{'标签':<16}git_sha")
    for snap in snapshots:
        rnd = rounds.get(snap.round_no)
        state = rnd.state.value if rnd is not None else "-"
        print(
            f"{snap.round_no:<6}{state:<14}{snap.gate_state.value:<11}"
            f"{snap.package_hash:<18}{(snap.tag or '-'):<16}{snap.git_sha or '-'}"
        )
    return EXIT_OK


def cmd_rollback(args: argparse.Namespace) -> int:
    """P0 灰度回滚：读快照 + 给出 git checkout 提示（不自动动 git）。"""
    store = Store(args.db)
    snap = store.get_snapshot(args.round_no)
    store.close()
    if snap is None:
        print(f"[kfpa] 轮次 {args.round_no} 没有快照，无法回滚", file=sys.stderr)
        return EXIT_ERROR

    print(f"轮次 {snap.round_no} 快照：package_hash={snap.package_hash}，"
          f"git_sha={snap.git_sha or '-'}，tag={snap.tag or '-'}")
    print("P0 灰度回滚（仅提示，不自动执行 git 操作）：")
    if snap.tag:
        print(f"  git checkout {snap.tag}")
    elif snap.git_sha:
        print(f"  git checkout {snap.git_sha}")
    else:
        print("  该快照未绑定 tag / git_sha，请人工确认要回到的版本")
    return EXIT_OK


# ---------------------------------------------------------------- 解析


def _register_tickets(parser: argparse.ArgumentParser, subparsers: Any) -> None:
    """把界面的 `tickets` 子命令组挂进来（模块缺失时静默跳过）。"""
    try:
        from .ui import cli as ui_cli
    except ImportError:
        return
    except Exception as exc:  # 界面模块自身炸了要让用户看见，但不能拖垮主 CLI
        print(f"[kfpa] 警告：ui 模块装载失败：{exc}", file=sys.stderr)
        return
    try:
        ui_cli.register(subparsers)
    except Exception as exc:
        print(f"[kfpa] 警告：tickets 子命令注册失败：{exc}", file=sys.stderr)


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

    p_init = subparsers.add_parser("init", help="建库建表（rounds/tickets/packages/gates/failures/snapshots）")
    p_init.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_init.set_defaults(handler=cmd_init)

    p_round = subparsers.add_parser("round", help="跑一轮：设计→質問票→任务包→执行→门禁→快照")
    p_round.add_argument("--design", required=True, help="原始设计文件（Markdown）")
    p_round.add_argument("--round", dest="round_no", type=int, required=True, help="轮次号（整数）")
    p_round.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="默认开启（P0 验收路径，用 MockExecutor）；--no-dry-run 走真实执行器",
    )
    p_round.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_round.add_argument("--workdir", default=".", help="执行器工作目录（默认当前目录）")
    p_round.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"角色配置（默认 {DEFAULT_CONFIG_PATH}）")
    p_round.add_argument("--provider", choices=["mock", "litellm"], default="mock", help="LLM 提供方（默认 mock）")
    p_round.add_argument("--max-attempts", type=int, default=3, help="门禁重试上限（默认 3）")
    p_round.set_defaults(handler=cmd_round)

    p_gate = subparsers.add_parser("gate", help="查看某轮最新门禁判定（退出码即门禁态）")
    p_gate.add_argument("round_no", type=int, help="轮次号")
    p_gate.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_gate.set_defaults(handler=cmd_gate)

    p_history = subparsers.add_parser("history", help="列轮次快照（轮次/状态/package_hash/门禁态）")
    p_history.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_history.set_defaults(handler=cmd_history)

    p_rollback = subparsers.add_parser("rollback", help="按快照回滚（P0：记录 + git checkout 提示）")
    p_rollback.add_argument("round_no", type=int, help="轮次号")
    p_rollback.add_argument("--db", default="kfpa.db", help="SQLite 路径（默认 kfpa.db）")
    p_rollback.set_defaults(handler=cmd_rollback)

    _register_tickets(parser, subparsers)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """返回退出码；`python -m kfpa` 与 console_scripts 都走这里。"""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / 参数错误：argparse 会 SystemExit，这里统一成返回值
        code = exc.code
        if code is None:
            return EXIT_OK
        return int(code) if isinstance(code, int) else EXIT_ERROR

    handler: Optional[Callable[[argparse.Namespace], int]] = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return handler(args)


__all__ = [
    "main",
    "build_parser",
    "EXIT_OK",
    "EXIT_NEEDS_FIX",
    "EXIT_BLOCKED",
    "EXIT_NEEDS_CONFIRM",
    "EXIT_ERROR",
]

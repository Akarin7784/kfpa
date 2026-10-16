"""快车道影响分析（P1，D3-3.6，纯函数）。

判据（design.md §5.6，全部可计算，**不调用任何模型**）：

① 不新增 / 不修改接口契约
② 不改数据模型（`src/kfpa/schemas.py` 与 `migrations/**` 未动）
③ 波及面 ≤ 1 个模块（按 `diff_files` 的顶层模块去重计数）

三条全真的才放行（`fast_lane=True`）；否则给出触碰的边界清单，并按 D3-3.6 打印
「升级回完整设计回合」。

① 的可计算口径（本模块定，写进 docstring 与 PR）：任务包的 `interface_contract` 段里
出现以「新增 / 修改 / 变更 / 删除 / + / -」开头的条目，即视为该包声明了契约增量。
（六段式任务包本来就**必须有** interface_contract 段，所以不能用「段非空」当判据。）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import TaskPackage

#: 契约增量的行首标记（对应判据①）
CONTRACT_DELTA_MARKS = ("新增", "修改", "变更", "删除", "+", "-", "＋", "－")

#: 数据模型文件（对应判据②）
DATA_MODEL_FILES = ("src/kfpa/schemas.py",)
DATA_MODEL_DIRS = ("migrations/",)

#: 模块顶层目录名（判据③的「模块」粒度）
_MODULE_ROOTS = ("src/kfpa/", "src/", "tests/", "scripts/", "docs/", "examples/")


@dataclass
class ImpactReport:
    """快车道影响分析结果（D3-3.6）。"""

    round_no: int
    fast_lane: bool
    boundaries: list[str] = field(default_factory=list)   # 触碰的边界清单（fast_lane=False 时非空）
    modules: list[str] = field(default_factory=list)      # 波及的模块（去重、稳定排序）
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "fast_lane=True（可走快车道）" if self.fast_lane else "fast_lane=False → 升级回完整设计回合"
        return head + ("；边界：" + "；".join(self.boundaries) if self.boundaries else "")


def top_module(path: str) -> str:
    """把 diff 文件路径归到「模块」：`src/kfpa/pipeline.py` → `kfpa`，`migrations/x.sql` → `migrations`。"""
    normal = str(path or "").strip().replace("\\", "/").lstrip("./")
    for root in _MODULE_ROOTS:
        if normal.startswith(root):
            rest = normal[len(root):]
            return rest.split("/", 1)[0] if "/" in rest else root.rstrip("/").split("/")[-1]
    parts = normal.split("/")
    return parts[0] if len(parts) > 1 else (parts[0] or "unknown")


def is_data_model_touch(path: str) -> bool:
    """判据②：`schemas.py` 或 `migrations/**` 被动过。"""
    normal = str(path or "").strip().replace("\\", "/").lstrip("./")
    return normal in DATA_MODEL_FILES or any(normal.startswith(d) for d in DATA_MODEL_DIRS)


def contract_delta_lines(package: TaskPackage) -> list[str]:
    """判据①：任务包 `interface_contract` 里声明了契约增量的条目。"""
    hits: list[str] = []
    for line in package.interface_contract or []:
        text = str(line).strip()
        if text.startswith(CONTRACT_DELTA_MARKS):
            hits.append(text)
    return hits


def analyze(task_package: TaskPackage, diff_files: list[str]) -> ImpactReport:
    """三判据量化分析；纯函数，同输入同输出。"""
    files = [str(f).strip().replace("\\", "/").lstrip("./") for f in (diff_files or []) if str(f).strip()]
    boundaries: list[str] = []
    reasons: list[str] = []

    # ① 接口契约
    deltas = contract_delta_lines(task_package)
    if deltas:
        boundaries.append("①接口契约：" + "；".join(deltas[:3]) + ("…" if len(deltas) > 3 else ""))

    # ② 数据模型
    model_files = [f for f in files if is_data_model_touch(f)]
    if model_files:
        boundaries.append("②数据模型：" + "、".join(sorted(set(model_files))))

    # ③ 波及面
    modules = sorted({top_module(f) for f in files})
    if len(modules) > 1:
        boundaries.append("③波及面 >1 个模块：" + "、".join(modules))

    fast_lane = not boundaries
    if fast_lane:
        reasons.append(
            f"[impact] 三判据全真（契约无增量、数据模型未动、波及 {len(modules)} 个模块）→ 可走快车道"
        )
    else:
        reasons.append(
            "[impact] 触碰设计边界（" + "；".join(boundaries) + "）→ 升级回完整设计回合（design.md §5.6）"
        )
    return ImpactReport(
        round_no=int(getattr(task_package, "round_no", 0) or 0),
        fast_lane=fast_lane,
        boundaries=boundaries,
        modules=modules,
        reasons=reasons,
    )


# ---------------------------------------------------------------- CLI 挂载（供 cli.py 调用）


def _collect_diff_files(repo: str | None = None, base: str | None = None) -> list[str]:
    """尽力从 git 取本轮改动文件；取不到返回空列表（不编造）。"""
    import subprocess

    cmd = ["git", "diff", "--name-only"]
    if base:
        cmd.append(str(base))
    try:
        proc = subprocess.run(
            cmd, cwd=repo or ".", capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def register(subparsers) -> None:  # pragma: no cover - 由 cli.py 挂载
    """注册 `kfpa fastlane check --round N [--base <git-ref>] [--json]`（D3-3.6）。"""
    parser = subparsers.add_parser("fastlane", help="快车道影响分析（D3-3.6）")
    sub = parser.add_subparsers(dest="fastlane_cmd", required=True)
    check = sub.add_parser("check", help="对某轮任务包做三判据影响分析")
    check.add_argument("--round", dest="round_no", type=int, required=True)
    check.add_argument("--db", default="kfpa.db")
    check.add_argument("--base", default=None, help="git 基线 ref（默认工作区未提交改动）")
    check.add_argument("--repo", default=None, help="git 仓库路径（默认当前目录）")
    check.add_argument("--json", action="store_true")

    def _run(args) -> int:
        from .schemas import dumps

        try:  # P1 D3-3.2：磐石提供 make_store() 工厂（PG/SQLite 双驱动）
            from .store import make_store

            store = make_store(getattr(args, "db", "kfpa.db"))
        except (ImportError, AttributeError):  # 兼容 P0 的 Store 类
            from .store import Store

            store = Store(getattr(args, "db", "kfpa.db"))
        package = store.get_package(args.round_no)
        if package is None:
            print(f"[impact] 第 {args.round_no} 轮没有任务包 → 先跑 `kfpa round`（退出码 3）")
            return 3
        report = analyze(package, _collect_diff_files(args.repo, args.base))
        if args.json:
            print(dumps(report))
        else:
            print(f"轮次 {report.round_no}：{report.summary()}")
            for line in report.reasons:
                print("  " + line)
        return 0

    check.set_defaults(handler=_run, func=_run)


__all__ = [
    "CONTRACT_DELTA_MARKS",
    "DATA_MODEL_FILES",
    "DATA_MODEL_DIRS",
    "ImpactReport",
    "top_module",
    "is_data_model_touch",
    "contract_delta_lines",
    "analyze",
    "register",
]

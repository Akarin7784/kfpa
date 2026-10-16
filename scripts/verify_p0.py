#!/usr/bin/env python
"""KFPA P0 独立验收台架（A1–A7）。

用法（仓库根目录，或任意目录加 --root）：
    python scripts/verify_p0.py                  # 跑全部断言
    python scripts/verify_p0.py --only A2,A5.1   # 只跑指定断言
    python scripts/verify_p0.py --json out.json  # 落机器可读证据
    python scripts/verify_p0.py --list           # 列出全部断言
    python scripts/verify_p0.py --keep-temp      # 保留临时库，便于手工复现

设计原则（对抗评审视角）：
1. 断言之间独立：各自建临时库、临时 workdir，不依赖执行顺序。
2. 模块缺失 != 通过：缺失按 FAIL 报，并指名缺哪个文件、归谁写。
3. 证据 = 真实命令 + 真实退出码 + 真实输出片段，不写「已通过」三个字。
4. 版本自适应：契约 v1.0 没有 ExecResult.results / error_category 时，
   依赖它们的子断言报 SKIP（既不算通过也不算失败），并提示等 v1.1 落 main。
5. 全程离线：只用 mock provider + 临时 SQLite，不联网、不要 API key、
   不导入 litellm / aider / streamlit。

退出码：0=无 FAIL（SKIP 不影响）；1=有 FAIL；2=台架自身用法错误。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 台架已对齐的契约版本；升版后由镜鉴同步（未对齐的版本直接 FAIL，防静默漂移）
SUPPORTED_CONTRACT_VERSIONS = ("1.0", "1.1")

#: A5 铁律的模型字符串扫描面
MODEL_STR_PAT = re.compile(r"deepseek|gpt-|claude|qwen|bge", re.I)

#: 模型字符串唯一允许出现的实现文件（适配器内部）
MODEL_STR_ALLOWED = ("src/kfpa/llm.py",)

#: p0-plan §2 约定的 SQLite 表
EXPECTED_TABLES = ("rounds", "tickets", "packages", "gates", "snapshots", "failures")

#: 契约文件 -> 唯一写者（用于失败时指名责任模块）
OWNERS = {
    "pyproject.toml": "磐石",
    "src/kfpa/__init__.py": "磐石",
    "src/kfpa/config.py": "磐石",
    "src/kfpa/store.py": "磐石",
    "src/kfpa/llm.py": "磐石",
    "src/kfpa/cli.py": "磐石",
    "src/kfpa/__main__.py": "磐石",
    "src/kfpa/pipeline.py": "长流",
    "src/kfpa/gates.py": "长流",
    "src/kfpa/executor.py": "长流",
    "src/kfpa/ui/cli.py": "界面",
    "examples/design-r1.md": "长流",
    "src/kfpa/schemas.py": "Chief（冻结）",
    "docs/dev/p0-plan.md": "Chief",
}

REQUIRED_P0_FILES = (
    "pyproject.toml",
    "src/kfpa/config.py",
    "src/kfpa/store.py",
    "src/kfpa/llm.py",
    "src/kfpa/cli.py",
    "src/kfpa/__main__.py",
    "src/kfpa/pipeline.py",
    "src/kfpa/gates.py",
    "src/kfpa/executor.py",
    "src/kfpa/ui/cli.py",
    "examples/design-r1.md",
)


# ------------------------------------------------------------------ 数据结构


@dataclass
class Evidence:
    cmd: str = ""
    rc: int | None = None
    stdout: str = ""
    stderr: str = ""
    note: str = ""
    verdict: bool = True


@dataclass
class Check:
    cid: str
    title: str
    owner: str
    reference: str
    status: str = "PASS"
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    def add(self, *, cmd: str = "", rc: int | None = None, stdout: str = "",
            stderr: str = "", ok: bool = True, note: str = "") -> bool:
        self.evidence.append(Evidence(cmd, rc, stdout, stderr, note, ok))
        if not ok:
            self.status = "FAIL"
        return ok

    def fail(self, msg: str) -> None:
        self.status = "FAIL"
        self.findings.append(msg)

    def warn(self, msg: str) -> None:
        self.findings.append("[建议] " + msg)

    def skip(self, msg: str) -> None:
        if self.status != "FAIL":
            self.status = "SKIP"
        self.findings.append("[SKIP] " + msg)


@dataclass
class Ctx:
    root: Path
    python: str
    tmp: Path
    keep: bool = False

    def __post_init__(self) -> None:
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(self.root / "src") + os.pathsep + self.env.get("PYTHONPATH", "")
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["PYTHONUTF8"] = "1"

    def run(self, cmd: list[str], *, cwd: Path | None = None, timeout: int = 180) -> dict:
        p = subprocess.run(
            cmd, cwd=str(cwd or self.root), env=self.env,
            capture_output=True, timeout=timeout,
        )
        return {
            "cmd": " ".join(cmd),
            "rc": p.returncode,
            "stdout": p.stdout.decode("utf-8", "replace"),
            "stderr": p.stderr.decode("utf-8", "replace"),
        }

    def fresh_db(self, name: str) -> Path:
        p = self.tmp / f"{name}.db"
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
        return p

    def design_text(self) -> str:
        f = self.root / "examples/design-r1.md"
        if f.exists():
            return f.read_text(encoding="utf-8", errors="replace")
        return "# 占位设计\n系统应支持轮次快照与質問票台账。\n"


def import_mod(name: str):
    """导入 src 下的模块；失败返回 (None, 中文诊断)。"""
    try:
        return importlib.import_module(name), None
    except Exception as exc:  # noqa: BLE001 - 验收台架必须吃掉一切导入异常
        return None, f"{type(exc).__name__}: {exc}"


def has_field(cls, name: str) -> bool:
    try:
        return name in getattr(cls, "__dataclass_fields__", {})
    except Exception:  # noqa: BLE001
        return False


def cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def excerpt(text: str, n: int = 700) -> str:
    text = (text or "").rstrip()
    if len(text) <= n:
        return text
    return text[: n // 2] + "\n      ...<输出截断>...\n" + text[-n // 2:]


def missing_hint(ctx: Ctx, cid: str) -> str:
    """给出「差哪些文件、归谁」的可定位诊断。"""
    missing = [f for f in REQUIRED_P0_FILES if not (ctx.root / f).exists()]
    if not missing:
        return f"{cid}：契约文件齐备，但行为不满足断言 —— 见上述命令证据。"
    lines = [f"{cid} 无法验收：缺 {len(missing)} 个契约文件（p0-plan §2）："]
    lines += [f"    - {f}    <- {OWNERS.get(f, '未登记')}" for f in missing]
    return "\n".join(lines)


def probe_db(db: Path) -> tuple[set[str], dict[str, int], str]:
    """原始 SQLite 读表名 + 行数，不经过被测代码。"""
    try:
        con = sqlite3.connect(str(db))
    except Exception as exc:  # noqa: BLE001
        return set(), {}, f"{type(exc).__name__}: {exc}"
    try:
        tables = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'").fetchall()}
        counts = {}
        for t in tables:
            try:
                counts[t] = con.execute(f'select count(*) from "{t}"').fetchone()[0]
            except Exception:  # noqa: BLE001
                counts[t] = -1
        return tables, counts, ""
    finally:
        con.close()


def seed_ticket(store_mod, db: Path, *, ticket_id: str, round_no: int, impact: str,
                status: str = "OPEN") -> str:
    """用 §3.2 契约 API 造票（不碰被测 CLI，保证 A3/A7 独立于 A2）。"""
    sc = importlib.import_module("kfpa.schemas")
    st = store_mod.Store(str(db))
    try:
        st.init_schema()
    except Exception:  # noqa: BLE001
        pass
    st.add_ticket(sc.QATicket(
        ticket_id=ticket_id,
        round_no=round_no,
        location="design.md §5.1",
        option_a="解释 A：按仓隔离",
        option_b="解释 B：全局共享",
        architect_preference="倾向 A",
        impact=sc.TicketImpact[impact],
        status=sc.TicketStatus[status],
    ))
    return ticket_id


# ------------------------------------------------------------------ A1


def check_a1(ctx: Ctx, c: Check) -> None:
    ev = ctx.run([ctx.python, "-m", "kfpa", "--help"])
    out = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == 0, note="验收命令 1：--help 期望退出码 0")
    if ev["rc"] == 0:
        for token in ("init", "round", "tickets", "history"):
            c.add(ok=token in out, note=f"`--help` 输出含子命令 `{token}`",
                  stdout=excerpt(out, 300))
    else:
        c.fail(missing_hint(ctx, "A1"))

    v = ctx.run([ctx.python, "-c",
                 "import kfpa.schemas as s; print(s.CONTRACT_VERSION)"])
    ver = v["stdout"].strip()
    c.add(cmd=v["cmd"], rc=v["rc"], stdout=v["stdout"], stderr=v["stderr"],
          ok=v["rc"] == 0 and ver in SUPPORTED_CONTRACT_VERSIONS,
          note=f"契约版本 = {ver or '(空)'}；台架已对齐 {SUPPORTED_CONTRACT_VERSIONS}")
    if ver and ver not in SUPPORTED_CONTRACT_VERSIONS:
        c.fail(f"契约版本 {ver} 未同步到验收台架 —— 升版必须同步 scripts/verify_p0.py"
               f" 的 SUPPORTED_CONTRACT_VERSIONS 与 docs/acceptance/。")


# ------------------------------------------------------------------ A2


def check_a2(ctx: Ctx, c: Check) -> None:
    db = ctx.fresh_db("a2")
    work = ctx.tmp / "a2-work"
    work.mkdir(parents=True, exist_ok=True)
    design = ctx.root / "examples/design-r1.md"
    if not design.exists():
        c.fail(f"缺 examples/design-r1.md（{OWNERS['examples/design-r1.md']}）—— "
               "A2 的 dry-run 输入不存在，无法验收。")

    ev1 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev1["cmd"], rc=ev1["rc"], stdout=ev1["stdout"], stderr=ev1["stderr"],
          ok=ev1["rc"] == 0, note="验收命令 2a：建库")
    if ev1["rc"] != 0:
        c.fail(missing_hint(ctx, "A2"))

    ev2 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(design),
                   "--round", "1", "--dry-run", "--db", str(db), "--workdir", str(work)])
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] == 0, note="验收命令 2b：整轮 dry-run（离线 mock）期望退出码 0")
    if ev2["rc"] != 0:
        tail = (ev2["stderr"] or ev2["stdout"]).strip().splitlines()
        c.fail("dry-run 未跑通。若输出/错误里出现「未关闭的高影响票」类信息，"
               "说明 MockLLM 剧本产出的票里含 HIGH 未关闭 —— 见 A2/A3 语义冲突"
               "（docs/acceptance/p0-acceptance.md「已知冲突」）。")
        if tail:
            c.findings.append("    末行退出信息: " + tail[-1][:200])

    tables, counts, err = probe_db(db)
    if err:
        c.add(ok=False, note=f"原始 SQLite 读取失败：{err}")
        c.fail("无法用 sqlite3 直接读库（A2 要求 SQLite 落库）。")
    else:
        c.add(ok=True, note=f"原始库表：{sorted(tables)}")
        for t in EXPECTED_TABLES:
            ok = t in tables
            c.add(ok=ok, note=f"表 `{t}` 存在（p0-plan §2）" + ("" if ok else " —— 缺失"))
            if not ok:
                c.fail(f"SQLite 缺表 `{t}`；实际表 = {sorted(tables)}")
        for t, minimum in (("rounds", 1), ("tickets", 1), ("packages", 1),
                           ("gates", 1), ("snapshots", 1)):
            n = counts.get(t, -1)
            c.add(ok=n >= minimum, note=f"表 `{t}` 行数 = {n}（要求 >= {minimum}）")
            if n < minimum:
                c.fail(f"表 `{t}` 行数 {n} < {minimum}：dry-run 未把该环节落库。")

    store_mod, serr = import_mod("kfpa.store")
    if store_mod is None:
        c.add(ok=False, note=f"import kfpa.store 失败：{serr}")
        c.fail(f"kfpa.store 不可导入（{OWNERS['src/kfpa/store.py']}）：{serr}")
        return
    st = store_mod.Store(str(db))
    # 票面五要素齐全
    try:
        tickets = st.list_tickets(1)
        c.add(ok=len(tickets) >= 1, note=f"Store.list_tickets(1) 返回 {len(tickets)} 张票")
        if len(tickets) < 1:
            c.fail("質問票 <1 张：轉譯环节未产出歧义票。")
        for t in tickets:
            miss = t.missing_fields()
            c.add(ok=not miss, note=f"票 {t.ticket_id} 五要素缺失 = {miss or '无'}")
            if miss:
                c.fail(f"票 {t.ticket_id} 缺票面要素 {miss}（design.md §5.1）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"list_tickets 异常：{type(exc).__name__}: {exc}")
        c.fail("Store.list_tickets 接口不可用（§3.2）。")
    # 任务包六段齐全
    try:
        pkg = st.get_package(1)
        c.add(ok=pkg is not None, note="Store.get_package(1) 有值")
        if pkg is None:
            c.fail("packages 表无第 1 轮任务包。")
        else:
            miss = pkg.missing_sections()
            c.add(ok=not miss, note=f"任务包缺段 = {miss or '无'}（六段，feedback 首轮可空）")
            if miss:
                c.fail(f"任务包缺段 {miss}（design.md §5.2 六段式）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"get_package 异常：{type(exc).__name__}: {exc}")
        c.fail("Store.get_package 接口不可用（§3.2）。")
    # 门禁有态 / 快照
    for fn, label in (("list_gate_reports", "门禁报告"), ("list_snapshots", "轮次快照")):
        try:
            rows = getattr(st, fn)(1) if fn == "list_gate_reports" else getattr(st, fn)()
            c.add(ok=len(rows) >= 1, note=f"Store.{fn}() 返回 {len(rows)} 条{label}")
            if len(rows) < 1:
                c.fail(f"{label}为空：§3.2 要求落库。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"{fn} 异常：{type(exc).__name__}: {exc}")
            c.fail(f"Store.{fn} 接口不可用（§3.2）。")


# ------------------------------------------------------------------ A3


def check_a3(ctx: Ctx, c: Check) -> None:
    store_mod, serr = import_mod("kfpa.store")
    pipe_mod, perr = import_mod("kfpa.pipeline")
    llm_mod, lerr = import_mod("kfpa.llm")
    if store_mod is None or pipe_mod is None or llm_mod is None:
        c.add(ok=False, note=f"导入失败：store={serr} pipeline={perr} llm={lerr}")
        c.fail(missing_hint(ctx, "A3"))
        return
    sc = importlib.import_module("kfpa.schemas")

    # --- A3.1 CLI 面：未关闭的高影响票 -> 退出码非零 + 中文错误
    db = ctx.fresh_db("a3")
    ev0 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev0["cmd"], rc=ev0["rc"], ok=ev0["rc"] == 0, note="A3 前置：建库")
    try:
        seed_ticket(store_mod, db, ticket_id="T-A3-HIGH", round_no=2, impact="HIGH")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"造高影响票失败：{type(exc).__name__}: {exc}")
        c.fail("无法用 Store.add_ticket 构造用例（§3.2）。")
        return
    ev = ctx.run([ctx.python, "-m", "kfpa", "round", "--design",
                  str(ctx.root / "examples/design-r1.md"), "--round", "2",
                  "--dry-run", "--db", str(db), "--workdir", str(ctx.tmp / "a3-work")])
    blob = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] != 0, note="存在未关闭 HIGH 票时，round 必须退出码非零（design.md §4 硬规则）")
    if ev["rc"] == 0:
        c.fail("硬规则被击穿：存在未关闭高影响票，`kfpa round` 仍然退出码 0。")
    c.add(ok=cjk(blob), note="拒绝理由必须是人能读的中文", stdout=excerpt(blob, 300))
    if not cjk(blob):
        c.fail("拒绝时的输出里没有中文提示（要求「中文错误」）。")

    # --- A3.2 库面：方向性两向断言（关票后必须放行）
    db2 = ctx.fresh_db("a3-lib")
    st = store_mod.Store(str(db2))
    try:
        st.init_schema()
    except Exception:  # noqa: BLE001
        pass
    try:
        llm = llm_mod.build_llm(llm_mod.load_roles(), provider="mock")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"build_llm(provider='mock') 失败：{type(exc).__name__}: {exc}")
        c.fail("MockLLM 装配失败（§3.3）。")
        return
    design_text = ctx.design_text()

    def _build(round_no: int):
        return pipe_mod.build_task_package(round_no, design_text, [], llm, [])

    # 有未关闭 HIGH 票 -> 必须拒绝
    try:
        seed_ticket(store_mod, db2, ticket_id="T-A3-HIGH-LIB", round_no=9, impact="HIGH")
        out = _build(9)
        refused = out is None or (
            getattr(out, "missing_sections", lambda: ["?"])() or not out.is_complete())
        c.add(ok=bool(refused), note=f"库面：有未关闭 HIGH 票时 build_task_package 返回 {type(out).__name__}")
        if not refused:
            c.fail("§3.4 硬规则被击穿：list_open_high_tickets 非空时 build_task_package 仍返回完整任务包。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=True, note=f"库面：有未关闭 HIGH 票时 build_task_package 抛出 {type(exc).__name__}: {exc}")
    # 关票后 -> 必须放行
    try:
        for t in st.list_open_high_tickets(9):
            st.close_ticket(t.ticket_id, "选 A：按仓隔离")
        out = _build(9)
        ok = out is not None and out.is_complete()
        c.add(ok=ok, note=f"库面：HIGH 票清零后 build_task_package 返回完整包 = {ok}")
        if not ok:
            c.fail("关闭高影响票后任务包仍不可生成（§3.4 要求放行）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"关票后 build_task_package 抛出 {type(exc).__name__}: {exc}")
        c.fail("关闭高影响票后仍拒绝生成任务包（§3.4 硬规则应只单向生效）。")


# ------------------------------------------------------------------ A4


def _complete_package(sc, round_no: int, *, with_prohibitions: bool = True):
    kw = dict(
        round_no=round_no,
        feature="轮次快照",
        goal="为每一轮生成可追溯的任务包快照",
        acceptance=[sc.AcceptanceCriterion(cid="AC-1", text="快照可查"),
                    sc.AcceptanceCriterion(cid="AC-2", text="package_hash 可复算")],
        boundaries=["空设计输入", "重复轮次号"],
        interface_contract=["Store.save_snapshot(snapshot) -> None"],
        prohibitions=["不得引入网络依赖"] if with_prohibitions else [],
        feedback=[],
    )
    return sc.TaskPackage(**kw)


def check_a4(ctx: Ctx, c: Check) -> None:
    gates_mod, gerr = import_mod("kfpa.gates")
    store_mod, serr = import_mod("kfpa.store")
    if gates_mod is None or store_mod is None:
        c.add(ok=False, note=f"导入失败：gates={gerr} store={serr}")
        c.fail(missing_hint(ctx, "A4"))
        return
    sc = importlib.import_module("kfpa.schemas")
    has_results = has_field(sc.ExecResult, "results")

    def new_store(tag: str):
        st = store_mod.Store(str(ctx.fresh_db(tag)))
        try:
            st.init_schema()
        except Exception:  # noqa: BLE001
            pass
        return st

    # A4.1 执行失败 -> NEEDS_FIX + 回灌区非空
    st = new_store("a4-1")
    pkg = _complete_package(sc, 1)
    fail_exec = sc.ExecResult(ok=False, error="mock 执行失败", executor="mock",
                              failed_criteria=["AC-1"])
    try:
        rpt = gates_mod.evaluate(1, pkg, fail_exec, st, attempt=1)
        c.add(ok=rpt.state is sc.GateState.NEEDS_FIX,
              note=f"exec.ok=False -> state={rpt.state.value}（期望 needs_fix）")
        if rpt.state is not sc.GateState.NEEDS_FIX:
            c.fail("§3.5-1：执行失败未判 NEEDS_FIX。")
        c.add(ok=bool(rpt.reasons), note=f"reasons={rpt.reasons}")
        c.add(ok="AC-1" in (rpt.failing_criteria or []),
              note=f"failing_criteria={rpt.failing_criteria}（应含执行器声明的 AC-1）")
        if "AC-1" not in (rpt.failing_criteria or []):
            c.fail("§3.5-1：failing_criteria 未回填执行器声明的失败项。")
        patterns = st.list_avoid_patterns(1)
        c.add(ok=bool(patterns), note=f"回灌区 list_avoid_patterns(1) = {patterns}")
        if not patterns:
            c.fail("§3.5-1：失败原因未写入 FailureRecord / 回避模式清单（回灌区为空）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"evaluate 异常：{type(exc).__name__}: {exc}")
        c.fail("§3.5 evaluate 接口不可用。")

    # A4.2 全过 -> PASS
    st = new_store("a4-2")
    ok_exec = sc.ExecResult(ok=True, diff="mock diff", executor="mock")
    if has_results:
        ok_exec.results = {"AC-1": True, "AC-2": True}
    try:
        rpt = gates_mod.evaluate(2, _complete_package(sc, 2), ok_exec, st, attempt=1)
        c.add(ok=rpt.state is sc.GateState.PASS, note=f"全过 -> state={rpt.state.value}（期望 pass）")
        if rpt.state is not sc.GateState.PASS:
            c.fail(f"§3.5-5：全过未判 PASS（reasons={rpt.reasons}）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"evaluate 异常：{type(exc).__name__}: {exc}")
        c.fail("§3.5 evaluate 接口不可用。")

    # A4.3 缺段本身是门禁失败
    st = new_store("a4-3")
    try:
        rpt = gates_mod.evaluate(3, _complete_package(sc, 3, with_prohibitions=False),
                                 ok_exec, st, attempt=1)
        c.add(ok=rpt.state is sc.GateState.NEEDS_FIX,
              note=f"缺 prohibitions 段 + exec.ok=True -> state={rpt.state.value}（期望 needs_fix）")
        if rpt.state is not sc.GateState.NEEDS_FIX:
            c.fail("§3.5-2：任务包缺段未被判 NEEDS_FIX。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"evaluate 异常：{type(exc).__name__}: {exc}")
        c.fail("§3.5 evaluate 接口不可用。")

    # A4.4 同类失败 >=2 次 -> BLOCKED + escalated
    st = new_store("a4-4")
    seq = []
    try:
        for attempt in (1, 2, 3):
            rpt = gates_mod.evaluate(4, _complete_package(sc, 4), fail_exec, st, attempt=attempt)
            seq.append((attempt, rpt.state.value, getattr(rpt, "same_reason_count", None),
                        getattr(rpt, "escalated", None)))
        last = rpt
        c.add(ok=bool(seq), note=f"三次同因失败的态序 = {seq}")
        ok = (last.state is sc.GateState.BLOCKED and bool(last.escalated)
              and int(last.same_reason_count or 0) >= 2)
        c.add(ok=ok, note=f"第三次 state={last.state.value} escalated={last.escalated} "
                          f"same_reason_count={last.same_reason_count}")
        if not ok:
            c.fail("§3.5-4：同类失败 >=2 次未升为 BLOCKED + escalated=True。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"evaluate 异常：{type(exc).__name__}: {exc}")
        c.fail("§3.5 evaluate 接口不可用。")

    # A4.5 版本门控：契约 v1.1 才有 results 字段
    if not has_results:
        c.skip("契约当前版本无 ExecResult.results / error_category（v1.0）；"
               "「断言未映射 G4」与错误分类子断言等 v1.1 落 main 后启用。")
    else:
        st = new_store("a4-5")
        ex = sc.ExecResult(ok=True, results={"AC-1": True, "AC-2": False}, executor="mock")
        try:
            rpt = gates_mod.evaluate(5, _complete_package(sc, 5), ex, st, attempt=1)
            c.add(ok=rpt.state is sc.GateState.NEEDS_FIX,
                  note=f"results 含 False -> state={rpt.state.value}（期望 needs_fix）")
            if rpt.state is not sc.GateState.NEEDS_FIX:
                c.fail("§3.5-3：验收断言未通过（results=False）却判 PASS。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"evaluate 异常：{type(exc).__name__}: {exc}")
            c.fail("§3.5 evaluate 未消费 ExecResult.results。")


# ------------------------------------------------------------------ A5


def check_a5_scan(ctx: Ctx, c: Check) -> None:
    src = ctx.root / "src"
    if not src.exists():
        c.fail("src/ 不存在。")
        return
    hits: list[tuple[str, int, str]] = []
    for p in sorted(src.rglob("*.py")):
        rel = p.relative_to(ctx.root).as_posix()
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if MODEL_STR_PAT.search(line):
                hits.append((rel, i, line.strip()))
    bad = [h for h in hits if h[0] not in MODEL_STR_ALLOWED]
    c.add(ok=not bad, note=f"src/ 命中 {len(hits)} 处模型字符串，其中越界 {len(bad)} 处；"
                           f"白名单 = {list(MODEL_STR_ALLOWED)}")
    for rel, i, line in hits[:20]:
        mark = "越界" if rel not in MODEL_STR_ALLOWED else "白名单"
        c.findings.append(f"[{mark}] {rel}:{i}: {line[:120]}")
    if bad:
        c.fail("A5 铁律被击穿：`src/` 业务代码里出现模型字符串 —— "
               + "; ".join(f"{r}:{i}" for r, i, _ in bad)
               + "。模型字符串只允许在 src/kfpa/llm.py 适配器与 config/models.yaml。")
    if not hits:
        c.findings.append("src/ 全库无模型字符串命中（铁律成立）。")


def check_a5_adapter(ctx: Ctx, c: Check) -> None:
    probe = r'''
import json, sys
out = {}
try:
    import kfpa.llm as m
except Exception as e:
    print(json.dumps({"import_error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False)); raise SystemExit(0)
out["litellm_imported"] = ("litellm" in sys.modules)
out["has_MockLLM"] = hasattr(m, "MockLLM")
out["has_LiteLLMAdapter"] = hasattr(m, "LiteLLMAdapter")
try:
    roles = m.load_roles()
    out["roles"] = sorted(roles.keys())
    out["roles_have_model"] = all(isinstance(v, dict) and v.get("model") for v in roles.values())
except Exception as e:
    out["roles_error"] = "%s: %s" % (type(e).__name__, e)
    roles = {}
try:
    llm = m.build_llm(roles, provider="mock")
    a = llm.complete("FLASH", "sys", "usr")
    b = llm.complete("FLASH", "sys", "usr")
    out["mock_call_ok"] = True
    out["mock_deterministic"] = (a == b)
    out["mock_returns_str"] = isinstance(a, str)
except Exception as e:
    out["mock_error"] = "%s: %s" % (type(e).__name__, e)
try:
    m.MockLLM().complete("BOGUS", "sys", "usr")
    out["bogus_role_rejected"] = False
except Exception as e:
    out["bogus_role_rejected"] = True
    out["bogus_role_exc"] = type(e).__name__
print(json.dumps(out, ensure_ascii=False))
'''
    ev = ctx.run([ctx.python, "-c", probe])
    c.add(cmd="python -c <A5.2 适配器探针>", rc=ev["rc"], stdout=ev["stdout"],
          stderr=ev["stderr"], ok=ev["rc"] == 0, note="导入 kfpa.llm 并做角色/惰性导入检查")
    if ev["rc"] != 0:
        c.fail(f"适配器探针自身失败（非 ImportError 也算）：rc={ev['rc']} "
               f"{excerpt(ev['stderr'], 300)}")
        return
    try:
        data = json.loads(ev["stdout"].strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"探针输出不是 JSON：{exc}")
        c.fail("无法解析适配器探针输出。")
        return
    if data.get("import_error"):
        c.add(ok=False, note=f"import kfpa.llm 失败：{data['import_error']}")
        c.fail(f"kfpa.llm 不可导入（{OWNERS['src/kfpa/llm.py']}）：{data['import_error']}")
        return
    spec_litellm = importlib.util.find_spec("litellm") if importlib.util else None
    c.add(ok=data.get("litellm_imported") is False,
          note=f"惰性导入：import kfpa.llm 后 litellm in sys.modules = "
               f"{data.get('litellm_imported')}（本机 litellm 已安装 = {spec_litellm is not None}）")
    if data.get("litellm_imported"):
        c.fail("§3.3：litellm 被顶层导入（缺包环境会 ImportError），必须惰性导入。")
    c.add(ok=bool(data.get("has_MockLLM")), note="MockLLM 存在（离线验收路径）")
    c.add(ok=bool(data.get("has_LiteLLMAdapter")), note="LiteLLMAdapter 存在（可选适配器）")
    c.add(ok=bool(data.get("roles_have_model")),
          note=f"load_roles() 角色 = {data.get('roles')}，每个角色含 model 字段")
    if data.get("roles_error"):
        c.add(ok=False, note=f"load_roles 异常：{data['roles_error']}")
        c.fail("§3.3 load_roles 不可用（应读 config/models.yaml）。")
        return
    want = {"ARCHITECT", "REVIEWER", "FLASH", "EMBEDDING"}
    got = set(data.get("roles") or [])
    c.add(ok=got == want, note=f"角色集合 = {sorted(got)}（期望 {sorted(want)}）")
    if got != want:
        c.fail(f"§3.3 角色集合不符：多 = {sorted(got - want)}，缺 = {sorted(want - got)}。")
    c.add(ok=bool(data.get("mock_call_ok")), note=f"MockLLM.complete 可用 = {data.get('mock_call_ok')}")
    if data.get("mock_error"):
        c.add(ok=False, note=f"MockLLM 调用异常：{data['mock_error']}")
        c.fail("§3.3 MockLLM 不可用（离线验收路径断）。")
    c.add(ok=bool(data.get("mock_deterministic")),
          note=f"MockLLM 同输入同输出 = {data.get('mock_deterministic')}（确定性要求）")
    if data.get("mock_call_ok") and data.get("mock_deterministic") is False:
        c.fail("§3.3 MockLLM 不确定性：同一 role/system/user 两次输出不同，快照无法复算。")
    c.add(ok=bool(data.get("bogus_role_rejected")),
          note=f"非法 role('BOGUS') 被拒绝 = {data.get('bogus_role_rejected')} "
               f"({data.get('bogus_role_exc')})")
    if not data.get("bogus_role_rejected"):
        c.fail("§3.3：非法 role 未被拒绝（只接受 ARCHITECT/REVIEWER/FLASH/EMBEDDING）。")


# ------------------------------------------------------------------ A6


def check_a6(ctx: Ctx, c: Check) -> None:
    db = ctx.fresh_db("a6")
    work = ctx.tmp / "a6-work"
    work.mkdir(parents=True, exist_ok=True)
    ev1 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev1["cmd"], rc=ev1["rc"], ok=ev1["rc"] == 0, note="A6 前置：建库")
    ev2 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design",
                   str(ctx.root / "examples/design-r1.md"), "--round", "1", "--dry-run",
                   "--db", str(db), "--workdir", str(work)])
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] == 0, note="A6 前置：整轮（快照在轮末产生）")
    if ev2["rc"] != 0:
        c.fail("整轮未跑通，A6 无法从真实轮次验收快照 —— 先修 A2/A3 冲突。")

    snap = {}
    try:
        store_mod = importlib.import_module("kfpa.store")
        st = store_mod.Store(str(db))
        snaps = st.list_snapshots()
        c.add(ok=len(snaps) >= 1, note=f"Store.list_snapshots() 返回 {len(snaps)} 条")
        if snaps:
            snap = snaps[-1]
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"list_snapshots 异常：{type(exc).__name__}: {exc}")
        c.fail(f"§3.2 Store.list_snapshots 不可用（{OWNERS['src/kfpa/store.py']}）。")

    tables, counts, err = probe_db(db)
    n = counts.get("snapshots", -1)
    c.add(ok=n >= 1, note=f"原始库 snapshots 行数 = {n}" + (f"；读取错误 {err}" if err else ""))
    if n < 1:
        c.fail("§4/A6：每轮必须落 snapshots 表，实际 0 行。")

    ev3 = ctx.run([ctx.python, "-m", "kfpa", "history", "--db", str(db)])
    blob = ev3["stdout"] + ev3["stderr"]
    c.add(cmd=ev3["cmd"], rc=ev3["rc"], stdout=ev3["stdout"], stderr=ev3["stderr"],
          ok=ev3["rc"] == 0 and bool(blob.strip()),
          note="`kfpa history` 期望退出码 0 且非空输出（§3.8：轮次/状态/package_hash/门禁态）")
    if ev3["rc"] != 0 or not blob.strip():
        c.fail("§3.8/A6：`kfpa history` 不可用或输出为空。")
        return
    ph = str(getattr(snap, "package_hash", "") or "")
    if ph:
        c.add(ok=ph[:8] in blob, note=f"history 输出含 package_hash 前 8 位 {ph[:8]}",
              stdout=excerpt(blob, 400))
        if ph[:8] not in blob:
            c.fail(f"§3.8：history 未列出 package_hash（快照哈希 {ph[:8]} 不在输出里）。")


# ------------------------------------------------------------------ A7


def _ensure_tickets(ctx: Ctx, db: Path) -> tuple[list[str], str]:
    """准备一批票：优先用真实 round，落不到就用 §3.2 API 造。"""
    rt = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    if rt["rc"] != 0:
        return [], f"init 失败：{excerpt(rt['stderr'] or rt['stdout'], 200)}"
    work = ctx.tmp / "a7-work"
    work.mkdir(parents=True, exist_ok=True)
    ctx.run([ctx.python, "-m", "kfpa", "round", "--design",
             str(ctx.root / "examples/design-r1.md"), "--round", "1", "--dry-run",
             "--db", str(db), "--workdir", str(work)])
    try:
        store_mod = importlib.import_module("kfpa.store")
    except Exception as exc:  # noqa: BLE001
        return [], f"import kfpa.store 失败：{type(exc).__name__}: {exc}"
    st = store_mod.Store(str(db))
    ids = []
    try:
        ids = [t.ticket_id for t in st.list_tickets()]
    except Exception:  # noqa: BLE001
        pass
    if not ids:
        try:
            seed_ticket(store_mod, db, ticket_id="T-A7-1", round_no=1, impact="MEDIUM")
            seed_ticket(store_mod, db, ticket_id="T-A7-2", round_no=1, impact="HIGH")
            ids = ["T-A7-1", "T-A7-2"]
        except Exception as exc:  # noqa: BLE001
            return [], f"造票失败（Store.add_ticket）：{type(exc).__name__}: {exc}"
    return ids, ""


def check_a7(ctx: Ctx, c: Check) -> None:
    db = ctx.fresh_db("a7")
    ids, err = _ensure_tickets(ctx, db)
    if err:
        c.add(ok=False, note=err)
        c.fail("A7 无法准备票数据：" + err)
        return
    tid = ids[0]
    c.add(ok=True, note=f"用例票 = {ids}")

    ev = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--db", str(db)])
    blob = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == 0, note="§3.7 台账可读；期望退出码 0")
    if ev["rc"] != 0:
        c.fail(f"`kfpa tickets list` 不可用（{OWNERS['src/kfpa/ui/cli.py']}）。")
        return
    c.add(ok=tid in blob, note=f"列表含票号 {tid}")
    if tid not in blob:
        c.fail(f"§3.7/A7：台账列表未列出票号 {tid}。")
    low = blob.lower()
    c.add(ok=any(k in low for k in ("high", "medium", "low", "高", "中", "低")),
          note="列表含影响面列", stdout=excerpt(blob, 400))
    c.add(ok=any(k in low for k in ("open", "closed", "dropped", "未关闭", "已关闭")),
          note="列表含状态列")

    # 过滤器（§3.7 契约签名）
    for flag, value in (("--round", "1"), ("--impact", "HIGH"), ("--status", "OPEN")):
        e = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", flag, value, "--db", str(db)])
        c.add(cmd=e["cmd"], rc=e["rc"], stdout=e["stdout"], stderr=e["stderr"],
              ok=e["rc"] == 0, note=f"过滤器 {flag} {value} 可用（§3.7）")
        if e["rc"] != 0:
            c.fail(f"§3.7：`tickets list {flag} {value}` 退出码 {e['rc']}。")

    # 关票 -> 入库
    e = ctx.run([ctx.python, "-m", "kfpa", "tickets", "close", tid,
                 "--answer", "选 A：按仓隔离", "--db", str(db)])
    c.add(cmd=e["cmd"], rc=e["rc"], stdout=e["stdout"], stderr=e["stderr"],
          ok=e["rc"] == 0, note="§3.7 关票并入库；期望退出码 0")
    if e["rc"] != 0:
        c.fail("§3.7/A7：`tickets close` 不可用。")
        return
    try:
        con = sqlite3.connect(str(db))
        row = con.execute("select status, answer from tickets where ticket_id = ?",
                          (tid,)).fetchone()
        con.close()
        c.add(ok=bool(row) and str(row[0]).lower().startswith("clos"),
              note=f"原始库 tickets 行 = {row}")
        if not row or not str(row[0]).lower().startswith("clos"):
            c.fail(f"§3.7/A7：关票未落库（ticket {tid} 状态 = {row[0] if row else '缺行'}）。")
        if row and not (row[1] or "").strip():
            c.fail("§3.7/A7：人工确认结论（answer）未落库。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"直读 tickets 表失败：{type(exc).__name__}: {exc}")
        c.fail("tickets 表不可直读。")

    e2 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--status", "OPEN",
                  "--db", str(db)])
    blob2 = e2["stdout"] + e2["stderr"]
    c.add(cmd=e2["cmd"], rc=e2["rc"], ok=e2["rc"] == 0 and tid not in blob2,
          note=f"关闭后 `--status OPEN` 不再列出 {tid}", stdout=excerpt(blob2, 300))
    if tid in blob2:
        c.fail("§3.7/A7：已关闭的票仍出现在 `--status OPEN` 列表里。")


def check_a7_batch(ctx: Ctx, c: Check) -> None:
    """附加断言（A7 表未列）：batch-close 的（§3.7 只说 --file，未定义文件格式）。"""
    db = ctx.fresh_db("a7b")
    ids, err = _ensure_tickets(ctx, db)
    if err:
        c.add(ok=False, note=err)
        c.fail("batch-close 前置失败：" + err)
        return
    y = ctx.tmp / "answers.yaml"
    j = ctx.tmp / "answers.json"
    y.write_text("".join(f"{i}: 选 A\n" for i in ids), encoding="utf-8")
    j.write_text(json.dumps([{"ticket_id": i, "answer": "选 A"} for i in ids],
                            ensure_ascii=False), encoding="utf-8")
    attempts = [
        ([ctx.python, "-m", "kfpa", "tickets", "batch-close", "--file", str(y), "--db", str(db)],
         "answers.yaml = {票号: 结论} 映射"),
    ]
    db2 = ctx.fresh_db("a7b2")
    ids2, err2 = _ensure_tickets(ctx, db2)
    if not err2:
        attempts.append(
            ([ctx.python, "-m", "kfpa", "tickets", "batch-close", "--file", str(j), "--db", str(db2)],
             "answers.json = [{ticket_id, answer}] 列表"))
    got = None
    for cmd, label in attempts:
        e = ctx.run(cmd)
        c.add(cmd=e["cmd"], rc=e["rc"], stdout=e["stdout"], stderr=e["stderr"],
              ok=e["rc"] == 0, note=f"尝试格式：{label}")
        if e["rc"] == 0:
            got = label
            break
    if got:
        c.add(ok=True, note=f"batch-close 接受的格式 = {got}")
    else:
        c.warn("`tickets batch-close` 未接受两种常见 answers 格式；§3.7 只写了 "
               "`--file <answers.yaml|json>` 未定义结构 —— 建议 Chief 在契约里补一行格式定义。"
               "（本条为使能性建议，不作为 A1–A7 阻塞项）")


# ------------------------------------------------------------------ 报告


def render(c: Check, verbose: bool) -> str:
    head = (f"== {c.cid} {c.title} == [{c.status}]  "
            f"责任: {c.owner}  依据: {c.reference}")
    lines = [head]
    for e in c.evidence:
        if e.cmd:
            mark = "OK" if e.verdict else "NG"
            lines.append(f"   $ {e.cmd}")
            lines.append(f"     -> rc={e.rc} [{mark}]" + (f"  {e.note}" if e.note else ""))
        elif e.note:
            lines.append(f"   - {'OK' if e.verdict else 'NG'} {e.note}")
        body = (e.stdout or "").strip()
        err = (e.stderr or "").strip()
        if body and (verbose or not e.verdict):
            lines.append("     输出: " + excerpt(body).replace("\n", "\n     "))
        if err and (verbose or not e.verdict):
            lines.append("     stderr: " + excerpt(err).replace("\n", "\n     "))
    for f in c.findings:
        lines.append("   ! " + f.replace("\n", "\n     "))
    return "\n".join(lines)


CHECKS = [
    ("A1", "CLI 可用：--help 退出码 0，含 init/round/tickets/history", "磐石+界面",
     "p0-plan §4/A1、§3.8", check_a1),
    ("A2", "离线端到端：dry-run 整轮，票五要素齐、任务包六段齐、落库五表", "磐石+长流",
     "p0-plan §4/A2、§3.2/§3.4", check_a2),
    ("A3", "硬规则：未关闭高影响票不得出任务包（CLI 非零 + 中文；关票后放行）", "长流",
     "p0-plan §4/A3、§3.4；design.md §4", check_a3),
    ("A4", "三态闭环：失败 NEEDS_FIX + 回灌区非空；同类失败 >=2 次 BLOCKED + escalated", "长流",
     "p0-plan §4/A4、§3.5；design.md §5.3", check_a4),
    ("A5.1", "模型绑定铁律：src/ 无模型字符串（llm.py 白名单外）", "全员",
     "p0-plan §4/A5", check_a5_scan),
    ("A5.2", "适配器：litellm 惰性导入、MockLLM 确定性、非法 role 拒绝", "磐石",
     "p0-plan §3.3", check_a5_adapter),
    ("A6", "快照可查：snapshots 有行，kfpa history 列出 package_hash", "磐石",
     "p0-plan §4/A6、§3.8", check_a6),
    ("A7", "台账可读写：tickets list 过滤可用、close 落库", "界面",
     "p0-plan §4/A7、§3.7", check_a7),
    ("A7.2", "附加：batch-close 攒批确认可用（A7 表未列）", "界面",
     "p0-plan §3.7", check_a7_batch),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KFPA P0 独立验收台架（A1–A7）")
    ap.add_argument("--root", default=str(REPO_ROOT), help="仓库根（默认脚本上级目录）")
    ap.add_argument("--only", default="", help="只跑指定断言，逗号分隔，支持前缀（如 A2,A5.1）")
    ap.add_argument("--json", dest="json_path", default="", help="把完整证据写到 JSON")
    ap.add_argument("--list", action="store_true", help="列出全部断言即退出")
    ap.add_argument("--keep-temp", action="store_true", help="保留临时库")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条证据的输出")
    args = ap.parse_args(argv)

    if args.list:
        for cid, title, owner, ref, _ in CHECKS:
            print(f"{cid:6s} [{owner}] {title}   ({ref})")
        return 0

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"台架错误：--root 不存在：{root}", file=sys.stderr)
        return 2

    selected = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in selected
               if not any(cid == s or cid.startswith(s + ".") for cid, *_ in CHECKS)]
    if unknown:
        print(f"台架错误：未知断言 {unknown}；用 --list 查看可用断言。", file=sys.stderr)
        return 2
    todo = [row for row in CHECKS
            if not selected or any(row[0] == s or row[0].startswith(s + ".") for s in selected)]

    tmpdir = Path(tempfile.mkdtemp(prefix="kfpa-verify-"))
    ctx = Ctx(root=root, python=sys.executable, tmp=tmpdir, keep=args.keep_temp)
    print(f"KFPA P0 验收台架  仓库={root}")
    print(f"python={ctx.python}")
    print(f"契约文件缺失={[f for f in REQUIRED_P0_FILES if not (root / f).exists()]}")
    print(f"临时目录={tmpdir}" + ("（--keep-temp）" if args.keep_temp else ""))
    print("-" * 72)

    results: list[Check] = []
    for cid, title, owner, ref, fn in todo:
        c = Check(cid=cid, title=title, owner=owner, reference=ref)
        try:
            fn(ctx, c)
        except Exception:  # noqa: BLE001
            c.fail("台架异常（断言实现自身出错，需人工看栈）：\n"
                   + traceback.format_exc(limit=6))
        results.append(c)
        print(render(c, args.verbose))
        print("-" * 72)

    n_pass = sum(1 for c in results if c.status == "PASS")
    n_fail = sum(1 for c in results if c.status == "FAIL")
    n_skip = sum(1 for c in results if c.status == "SKIP")
    print(f"汇总：{len(results)} 条断言 -> PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}")
    for c in results:
        if c.status != "PASS":
            print(f"  [{c.status}] {c.cid} {c.title}  (责任: {c.owner})")

    if args.json_path:
        payload = {
            "root": str(root),
            "python": ctx.python,
            "contract_versions_supported": list(SUPPORTED_CONTRACT_VERSIONS),
            "missing_files": [f for f in REQUIRED_P0_FILES if not (root / f).exists()],
            "summary": {"total": len(results), "pass": n_pass, "fail": n_fail, "skip": n_skip},
            "checks": [
                {
                    "cid": c.cid, "title": c.title, "owner": c.owner,
                    "reference": c.reference, "status": c.status,
                    "findings": c.findings,
                    "evidence": [
                        {"cmd": e.cmd, "rc": e.rc, "verdict": e.verdict, "note": e.note,
                         "stdout": (e.stdout or "")[:4000], "stderr": (e.stderr or "")[:4000]}
                        for e in c.evidence
                    ],
                }
                for c in results
            ],
        }
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 证据 -> {args.json_path}")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

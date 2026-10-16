#!/usr/bin/env python
"""KFPA P0 独立验收台架（A1–A9）。

用法（R-2：统一 PYTHONPATH=src，不做 editable 安装）：
    python scripts/verify_p0.py                  # 跑全部断言
    python scripts/verify_p0.py --only A2,A8     # 只跑指定断言（支持前缀）
    python scripts/verify_p0.py --json out.json  # 落机器可读证据，供 PR 评论引用
    python scripts/verify_p0.py --list           # 列出全部断言
    python scripts/verify_p0.py --keep-temp      # 保留临时库，便于手工复现

依据（效力从高到低）：docs/dev/p0-rulings.md(D2) > p0-design-addendum.md(D1) > p0-plan.md §4。

设计原则（对抗评审视角）：
1. 断言之间独立：各自建临时库、临时 workdir，不依赖执行顺序。
2. 模块缺失 != 通过：缺失按 FAIL 报，并指名缺哪个文件、归谁写；SKIP 只用于
   「本机环境不具备该验收前提」且必须在报告里显式可见。
3. 证据 = 真实命令 + 真实退出码 + 真实输出片段，不写「已通过」三个字。
4. 契约版本自适应：v1.0 没有 results/error_category/reason_key 时相关子断言 SKIP，
   但 main 已升 1.1，落在 1.0 的分支会被点名（PR 纪律：先 rebase）。
5. 全程离线：只用 mock provider + 临时 SQLite，不联网、不要 API key。

退出码：0=无 FAIL（SKIP 不影响）；1=有 FAIL；2=台架自身用法错误。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
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

#: 台架已对齐的契约版本（main 已升 1.2，见 docs/dev/p1-plan.md D3-3.1）
CONTRACT_VERSION_LATEST = "1.2"
SUPPORTED_CONTRACT_VERSIONS = ("1.0", "1.1", "1.2")

#: D1-5 / R-1 退出码表
EXIT_PASS, EXIT_USAGE, EXIT_CONFIRM, EXIT_DEP, EXIT_IO, EXIT_NEEDS_FIX, EXIT_BLOCKED = \
    0, 2, 3, 4, 5, 10, 11

#: A5 铁律扫描面
MODEL_STR_PAT = re.compile(r"deepseek|gpt-|claude|qwen|bge", re.I)
MODEL_STR_ALLOWED = ("src/kfpa/llm.py",)

#: D1-9：六表 + meta（实际建库 7 张）
EXPECTED_TABLES = ("meta", "rounds", "tickets", "task_packages", "gate_reports",
                   "failures", "snapshots")
#: D1-9 明确要求存在的列（抽查最容易被偷工减料的几处）
EXPECTED_COLUMNS = {
    "tickets": {"ticket_id", "round_no", "location", "option_a", "option_b",
                "architect_preference", "impact", "status", "answer", "created_at"},
    "task_packages": {"round_no", "package_hash", "payload", "created_at"},
    "gate_reports": {"round_no", "attempt", "state", "reason_key", "reasons",
                     "failing_criteria", "escalated", "same_reason_count"},
    "failures": {"round_no", "attempt", "reason", "avoid_pattern", "reason_key"},
    "snapshots": {"round_no", "package_hash", "gate_state", "git_sha", "tag"},
    "meta": {"key", "value"},
}

OWNERS = {
    "pyproject.toml": "磐石", "src/kfpa/__init__.py": "磐石", "src/kfpa/config.py": "磐石",
    "src/kfpa/store.py": "磐石", "src/kfpa/llm.py": "磐石", "src/kfpa/cli.py": "磐石",
    "src/kfpa/__main__.py": "磐石", "src/kfpa/pipeline.py": "长流",
    "src/kfpa/graph.py": "长流", "src/kfpa/gates.py": "长流", "src/kfpa/executor.py": "长流",
    "src/kfpa/prompts/": "长流", "examples/design-r1.md": "长流",
    "src/kfpa/ui/cli.py": "界面", "src/kfpa/schemas.py": "Chief（冻结）",
    "docs/dev/p0-plan.md": "Chief", "docs/dev/p0-design-addendum.md": "Chief",
    "docs/dev/p0-rulings.md": "Chief", "tests/test_graph_smoke.py": "镜鉴",
}

REQUIRED_P0_FILES = (
    "pyproject.toml", "src/kfpa/config.py", "src/kfpa/store.py", "src/kfpa/llm.py",
    "src/kfpa/cli.py", "src/kfpa/__main__.py", "src/kfpa/pipeline.py",
    "src/kfpa/gates.py", "src/kfpa/executor.py", "src/kfpa/ui/cli.py",
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
        sys.path.insert(0, str(self.root / "src"))

    def run(self, cmd: list[str], *, cwd: Path | None = None, timeout: int = 240,
            clean_env: bool = False) -> dict:
        """clean_env=True：不设 PYTHONIOENCODING / PYTHONUTF8（R-18：A10 与 A1/A7 复跑必须有一遍如此）。"""
        env = self.env
        if clean_env:
            env = {k: v for k, v in self.env.items()
                   if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        try:
            p = subprocess.run(cmd, cwd=str(cwd or self.root), env=env,
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"cmd": " ".join(cmd), "rc": -9, "stdout": "",
                    "stderr": f"命令超时（>{timeout}s）——离线验收不应长阻塞",
                    "clean_env": clean_env}
        return {"cmd": " ".join(cmd), "rc": p.returncode,
                "stdout": p.stdout.decode("utf-8", "replace"),
                "stderr": p.stderr.decode("utf-8", "replace"),
                "clean_env": clean_env}

    def fresh_db(self, name: str) -> Path:
        p = self.tmp / f"{name}.db"
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
        return p

    def design(self) -> Path:
        return self.root / "examples/design-r1.md"

    def design_text(self) -> str:
        f = self.design()
        return f.read_text(encoding="utf-8", errors="replace") if f.exists() else "# 占位设计\n"


def import_mod(name: str):
    """导入 src 下的模块；失败返回 (None, 中文诊断)。"""
    try:
        return importlib.import_module(name), None
    except Exception as exc:  # noqa: BLE001 - 验收台架必须吃掉一切导入异常
        return None, f"{type(exc).__name__}: {exc}"


def cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def excerpt(text: str, n: int = 700) -> str:
    text = (text or "").rstrip()
    if len(text) <= n:
        return text
    return text[: n // 2] + "\n      ...<输出截断>...\n" + text[-n // 2:]


def missing_hint(ctx: Ctx, cid: str) -> str:
    missing = [f for f in REQUIRED_P0_FILES if not (ctx.root / f).exists()]
    if not missing:
        return f"{cid}：契约文件齐备，但行为不满足断言 —— 见上述命令证据。"
    lines = [f"{cid} 无法验收：缺 {len(missing)} 个契约文件（p0-plan §2）："]
    lines += [f"    - {f}    <- {OWNERS.get(f, '未登记')}" for f in missing]
    return "\n".join(lines)


def probe_db(db: Path) -> tuple[set[str], dict[str, int], dict[str, set[str]], str]:
    """原始 SQLite 读表名 + 行数 + 列名，不经过被测代码。"""
    try:
        con = sqlite3.connect(str(db))
    except Exception as exc:  # noqa: BLE001
        return set(), {}, {}, f"{type(exc).__name__}: {exc}"
    try:
        tables = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'").fetchall()}
        counts, cols = {}, {}
        for t in tables:
            try:
                counts[t] = con.execute(f'select count(*) from "{t}"').fetchone()[0]
                cols[t] = {r[1] for r in con.execute(f'pragma table_info("{t}")').fetchall()}
            except Exception:  # noqa: BLE001
                counts[t], cols[t] = -1, set()
        return tables, counts, cols, ""
    finally:
        con.close()


def last_json_line(blob: str):
    """D1-10：--json 时最后输出 schemas.dumps(RoundResult) 单行 JSON。"""
    for line in reversed((blob or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return None


def status_value(status: object) -> str:
    """枚举或字符串统一取小写值（str(TicketStatus.CLOSED) 会得到 'TicketStatus.CLOSED'）。"""
    return str(getattr(status, "value", status)).strip().lower()


def _norm_key_part(text: object) -> str:
    """D1-4 规范化：小写、压空白、路径分隔统一 /、剔除行号与时间戳。"""
    s = str(text).lower().replace("\\", "/")
    s = re.sub(r":\d+|#\d+|\d{10,}", "", s)
    return re.sub(r"\s+", " ", s).strip()


def spec_reason_key(category: str, target: str) -> str:
    """D1-4：reason_key = sha1(f"{category}|{target}")[:12]。"""
    return hashlib.sha1(
        f"{_norm_key_part(category)}|{_norm_key_part(target)}".encode("utf-8")
    ).hexdigest()[:12]


def make_store_for(store_mod, db) -> object:
    """兼容两种 Store 工厂形态：P0 的 `Store(path)` 与 P1 的 `make_store(dsn)`（D3 §3.2）。

    P1 把 `Store` 改画成 Protocol 之后，直接 `Store(path)` 会立刻炸；优先走 `make_store`，
    保证 P0 门禁在 store 双驱动重构后不回归（D3 §6.2）。
    """
    factory = getattr(store_mod, "make_store", None)
    if callable(factory):
        try:
            st = factory(str(db))
            if st is not None:
                return st
        except Exception:  # noqa: BLE001
            pass
    return store_mod.Store(str(db))


def seed_ticket(store_mod, db: Path, *, ticket_id: str, round_no: int, impact: str,
                status: str = "OPEN", location: str = "design.md §5.1") -> str:
    """用 §3.2 契约 API 造票（不碰被测 CLI，保证 A3/A7 独立于 mock 剧本）。"""
    sc = importlib.import_module("kfpa.schemas")
    st = make_store_for(store_mod, db)
    try:
        st.init_schema()
    except Exception:  # noqa: BLE001
        pass
    st.add_ticket(sc.QATicket(
        ticket_id=ticket_id, round_no=round_no, location=location,
        option_a="解释 A：按仓隔离", option_b="解释 B：全局共享",
        architect_preference="倾向 A", impact=sc.TicketImpact[impact],
        status=sc.TicketStatus[status]))
    return ticket_id


# ------------------------------------------------------------------ A1


def check_a1(ctx: Ctx, c: Check) -> None:
    ev = ctx.run([ctx.python, "-m", "kfpa", "--help"])
    out = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS, note="A1：--help 期望退出码 0（R-1）")
    if ev["rc"] != EXIT_PASS:
        c.fail(missing_hint(ctx, "A1"))
    for token in ("init", "round", "tickets", "history"):
        c.add(ok=token in out, note=f"`--help` 含子命令 `{token}`", stdout=excerpt(out, 300))
        if token not in out:
            c.fail(f"§3.8：`--help` 未列子命令 `{token}`。")
    # R-1：退出码表必须出现在 --help 里
    codes_present = [code for code in ("10", "11", "3", "4", "5") if code in out]
    c.add(ok=len(codes_present) >= 4,
          note=f"`--help` 退出码表可见性：命中码 {codes_present}（R-1 要求列出 0/2/3/4/5/10/11）",
          stdout=excerpt(out, 400))
    if len(codes_present) < 4:
        c.fail("R-1：`--help` 的 epilog 必须列退出码表（0/2/3/4/5/10/11）。")

    v = ctx.run([ctx.python, "-c", "import kfpa.schemas as s; print(s.CONTRACT_VERSION)"])
    ver = v["stdout"].strip()
    c.add(cmd=v["cmd"], rc=v["rc"], stdout=v["stdout"], ok=v["rc"] == 0,
          note=f"契约版本 = {ver or '(空)'}；台架对齐 {CONTRACT_VERSION_LATEST}")
    if ver and ver not in SUPPORTED_CONTRACT_VERSIONS:
        c.fail(f"契约版本 {ver} 未同步到验收台架（SUPPORTED={SUPPORTED_CONTRACT_VERSIONS}）。")
    elif ver and ver < CONTRACT_VERSION_LATEST:
        c.warn(f"契约版本 {ver} < main 的 {CONTRACT_VERSION_LATEST}：该分支可能未 rebase 到 main"
               "（PR 纪律第 6 条），v1.1 字段相关断言会 SKIP。")

    # R-18/R-18.4：这一遍**不设** PYTHONIOENCODING / PYTHONUTF8（否则 A10 这类缺陷永远测不出）
    ev_clean = ctx.run([ctx.python, "-m", "kfpa", "--help"], clean_env=True)
    blob_clean = ev_clean["stdout"] + ev_clean["stderr"]
    c.add(cmd=ev_clean["cmd"] + "    [clean-env: 未设 PYTHONIOENCODING/PYTHONUTF8]",
          rc=ev_clean["rc"], stdout=ev_clean["stdout"], stderr=ev_clean["stderr"],
          ok=ev_clean["rc"] == EXIT_PASS and "UnicodeEncodeError" not in blob_clean,
          note="R-18：重定向且不设 PYTHONIOENCODING 时 --help 也不得编码崩")
    if ev_clean["rc"] != EXIT_PASS or "UnicodeEncodeError" in blob_clean:
        c.fail(f"R-18：不设 PYTHONIOENCODING 时 `--help` rc={ev_clean['rc']}"
               f"{'（UnicodeEncodeError）' if 'UnicodeEncodeError' in blob_clean else ''}"
               "—— 中文输出必须由 cli.main() 兜底 reconfigure 成 UTF-8。")


# ------------------------------------------------------------------ A2


def check_a2(ctx: Ctx, c: Check) -> None:
    db = ctx.fresh_db("a2")
    work = ctx.tmp / "a2-work"
    work.mkdir(parents=True, exist_ok=True)
    if not ctx.design().exists():
        c.fail(f"缺 examples/design-r1.md（{OWNERS['examples/design-r1.md']}）—— "
               "D1-7 的机读行在里面，A2 无法验收。")

    ev1 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev1["cmd"], rc=ev1["rc"], stdout=ev1["stdout"], stderr=ev1["stderr"],
          ok=ev1["rc"] == EXIT_PASS, note="A2-1：建库（R-1 期望 0）")
    if ev1["rc"] != EXIT_PASS:
        c.fail(missing_hint(ctx, "A2"))

    ev2 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "1", "--dry-run", "--json", "--db", str(db),
                   "--workdir", str(work)])
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] == EXIT_PASS, note="A2-2：整轮 dry-run（离线 mock）期望退出码 0")
    if ev2["rc"] != EXIT_PASS:
        tail = (ev2["stderr"] or ev2["stdout"]).strip().splitlines()
        c.fail("dry-run 未跑通（期望退出码 0）。")
        if tail:
            c.findings.append("    末行退出信息: " + tail[-1][:200])

    rr = last_json_line(ev2["stdout"])
    c.add(ok=rr is not None, note="D1-10：`--json` 末行是 schemas.dumps(RoundResult)")
    if rr is None:
        c.fail("D1-10：`--json` 未输出可解析的 RoundResult 单行 JSON。")

    tables, counts, cols, err = probe_db(db)
    if err:
        c.add(ok=False, note=f"原始 SQLite 读取失败：{err}")
        c.fail("无法用 sqlite3 直接读库（A2 要求 SQLite 落库）。")
    else:
        for t in EXPECTED_TABLES:
            ok = t in tables
            c.add(ok=ok, note=f"D1-9 表 `{t}` 存在" + ("" if ok else f" —— 缺失；实际 = {sorted(tables)}"))
            if not ok:
                c.fail(f"SQLite 缺表 `{t}`（D1-9）。")
        for t, minimum in (("meta", 1), ("rounds", 1), ("tickets", 1), ("task_packages", 1),
                           ("gate_reports", 1), ("snapshots", 1)):
            n = counts.get(t, -1)
            c.add(ok=n >= minimum, note=f"表 `{t}` 行数 = {n}（要求 >= {minimum}）")
            if n < minimum:
                c.fail(f"表 `{t}` 行数 {n} < {minimum}：dry-run 未把该环节落库。")
        for t, want in EXPECTED_COLUMNS.items():
            got = cols.get(t, set())
            if not got:
                continue
            missing = sorted(want - got)
            c.add(ok=not missing, note=f"表 `{t}` 缺列 = {missing or '无'}")
            if missing:
                c.fail(f"D1-9：表 `{t}` 缺列 {missing}。")

    # 执行结果必须带 results（A2 表明确要求）
    if rr:
        ex = rr.get("exec_result") or {}
        results = ex.get("results") or {}
        c.add(ok=bool(results), note=f"exec_result.results = {results}")
        if not results:
            c.fail("A2：执行结果缺 `results`（cid→bool），门禁 G4 无法判断言未映射。")
        gate = rr.get("gate") or {}
        c.add(ok=bool(gate.get("state")), note=f"RoundResult.gate.state = {gate.get('state')}")
        if not gate.get("state"):
            c.fail("A2：门禁无态（RoundResult.gate.state 空）。")
        tp = rr.get("task_package") or {}
        if tp:
            miss = [k for k in ("goal", "acceptance", "boundaries", "interface_contract",
                                "prohibitions") if not tp.get(k)]
            c.add(ok=not miss, note=f"RoundResult 任务包缺段 = {miss or '无'}（feedback 首轮可空）")
            if miss:
                c.fail(f"A2：任务包缺段 {miss}（design.md §5.2 六段式）。")

    store_mod, serr = import_mod("kfpa.store")
    if store_mod is None:
        c.add(ok=False, note=f"import kfpa.store 失败：{serr}")
        c.fail(f"kfpa.store 不可导入（{OWNERS['src/kfpa/store.py']}）：{serr}")
        return
    st = make_store_for(store_mod, db)
    try:
        tickets = st.list_tickets(1)
        c.add(ok=len(tickets) >= 1, note=f"Store.list_tickets(1) 返回 {len(tickets)} 张票")
        if len(tickets) < 1:
            c.fail("質問票 <1 张：D1-1 转译环节未产出歧义票。")
        for t in tickets:
            miss = t.missing_fields()
            c.add(ok=not miss, note=f"票 {t.ticket_id} 五要素缺失 = {miss or '无'}")
            if miss:
                c.fail(f"票 {t.ticket_id} 缺票面要素 {miss}（design.md §5.1）。")
            if not re.fullmatch(r"R\d+-\d{3}", str(t.ticket_id)):
                c.add(ok=False, note=f"票号 {t.ticket_id} 不符 D1-1 规则 R{round}-{{seq:03d}}")
                c.fail(f"D1-1：票号 `{t.ticket_id}` 不是 `R{{round}}-{{seq:03d}}`（pipeline 负责编号）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"list_tickets 异常：{type(exc).__name__}: {exc}")
        c.fail("Store.list_tickets 接口不可用（§3.2）。")
    try:
        pkg = st.get_package(1)
        c.add(ok=pkg is not None, note="Store.get_package(1) 有值")
        if pkg is None:
            c.fail("task_packages 表无第 1 轮任务包。")
        else:
            miss = pkg.missing_sections()
            c.add(ok=not miss, note=f"任务包缺段 = {miss or '无'}")
            if miss:
                c.fail(f"任务包缺段 {miss}（design.md §5.2）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"get_package 异常：{type(exc).__name__}: {exc}")
        c.fail("Store.get_package 接口不可用（§3.2）。")
    try:
        reports = st.list_gate_reports(1)
        c.add(ok=len(reports) >= 1, note=f"Store.list_gate_reports(1) 返回 {len(reports)} 条")
        if not reports:
            c.fail("门禁报告为空：D1-3 要求落库可复盘。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"list_gate_reports 异常：{type(exc).__name__}: {exc}")
        c.fail("Store.list_gate_reports 接口不可用（§3.2）。")


# ------------------------------------------------------------------ A3


def check_a3(ctx: Ctx, c: Check) -> None:
    """R-10：A3 纯 CLI 可复现 —— 用 `--inject high_ticket` 造 HIGH 票，不依赖私有 API。"""
    store_mod, serr = import_mod("kfpa.store")
    if store_mod is None:
        c.add(ok=False, note=f"import kfpa.store 失败：{serr}")
        c.fail(missing_hint(ctx, "A3"))
        return

    db = ctx.fresh_db("a3")
    ev0 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev0["cmd"], rc=ev0["rc"], ok=ev0["rc"] == EXIT_PASS, note="A3 前置：建库")

    base = [ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
            "--round", "2", "--dry-run", "--db", str(db), "--workdir", str(ctx.tmp / "a3-work")]
    ev = ctx.run(base[:4] + ["--inject", "high_ticket"] + base[4:])
    blob = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_CONFIRM,
          note="A3/R-10/D1-5：`--inject high_ticket` → 未关闭 HIGH 票 → 退出码 3")
    if ev["rc"] != EXIT_CONFIRM:
        c.fail(f"硬规则未按 D1-5 报码：期望退出码 3，实际 {ev['rc']}。"
               "（若报 2，先查 `--inject` 值域是否含 `high_ticket`——R-10 要求。）")
    c.add(ok=cjk(blob), note="拒绝理由必须是中文（D1-10：模块+原因+下一步）",
          stdout=excerpt(blob, 300))
    if not cjk(blob):
        c.fail("D1-10：拒绝输出没有中文提示。")

    # R-13：拦停时 rounds.state 必须是 tickets_open（这个时点可观测）
    try:
        rd0 = make_store_for(store_mod, db).get_round(2)
        s0 = status_value(rd0.state) if rd0 is not None else ""
    except Exception as exc:  # noqa: BLE001
        s0 = f"<{type(exc).__name__}: {exc}>"
    c.add(ok=s0 == "tickets_open",
          note=f"R-13：拦停时 rounds.state(round 2) = {s0!r}（期望 'tickets_open'）")
    if s0 != "tickets_open":
        c.fail(f"R-13：有未关闭 HIGH 票被拦停时 rounds.state 应为 tickets_open，实际 {s0!r}。")

    # R-10：`tickets list` 要能看到那张 HIGH 票
    el = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--impact", "HIGH",
                  "--db", str(db)])
    lout = el["stdout"] + el["stderr"]
    c.add(cmd=el["cmd"], rc=el["rc"], stdout=el["stdout"], stderr=el["stderr"],
          ok=el["rc"] == EXIT_PASS, note="R-10：台账可见注入的高影响票")
    ids = re.findall(r"R\d+-\d{3}", lout)
    c.add(ok=bool(ids), note=f"从 `tickets list --impact HIGH` 抽到票号 {sorted(set(ids))}")
    if not ids:
        c.add(ok=False, note=f"台账输出：{excerpt(lout, 300)}")
        c.fail("R-10/A3：`--inject high_ticket` 后 `tickets list --impact HIGH` 看不到 HIGH 票。")
        return
    tid = ids[0]

    evc = ctx.run([ctx.python, "-m", "kfpa", "tickets", "close", tid,
                   "--answer", "选 A：按仓隔离", "--db", str(db)])
    c.add(cmd=evc["cmd"], rc=evc["rc"], stdout=evc["stdout"], stderr=evc["stderr"],
          ok=evc["rc"] == EXIT_PASS, note=f"A3：`tickets close {tid}`（期望 0）")
    if evc["rc"] != EXIT_PASS:
        c.fail("A3：「关闭后可继续」的前提不成立 —— `tickets close` 不可用。")
        return
    ev2 = ctx.run(base)
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] in (EXIT_PASS, EXIT_NEEDS_FIX, EXIT_BLOCKED),
          note="A3：关票后重跑同一轮必须放行（不再被硬规则拦停）")
    if ev2["rc"] == EXIT_CONFIRM:
        c.fail("A3：关票后仍被硬规则拦停（退出码 3）—— 关闭状态没生效。")
    try:
        st = make_store_for(store_mod, db)
        t = st.get_ticket(tid)
        st_val = status_value(t.status) if t is not None else ""
        c.add(ok=t is not None and st_val == "closed" and bool((t.answer or "").strip()),
              note=f"R-6：重跑后票 {tid} status={st_val!r} answer={getattr(t, 'answer', None)!r}")
        if t is None or st_val != "closed":
            c.fail("R-6：同票号重跑把 status 冲掉了（必须保留 status/answer）。")
        # R-13：被拦停那轮 → tickets_open；关票放行后 → released
        try:
            rd = st.get_round(2)
            r2 = status_value(rd.state) if rd is not None else ""
        except Exception:  # noqa: BLE001
            r2 = ""
        c.add(ok=r2 in ("released", "executing", "gated", "snapshotted"),
              note=f"R-13：放行后 rounds.state(round 2) = {r2!r}"
                   f"（允许 released/executing/gated/snapshotted；RELEASED 是放行时点的瞬态）")
        if r2 not in ("released", "executing", "gated", "snapshotted"):
            c.fail(f"R-13：关票后重跑仍停在 {r2!r}（不应是 tickets_open/blocked）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"get_ticket 异常：{type(exc).__name__}: {exc}")
        c.fail("Store.get_ticket 接口不可用（§3.2）。")

    # A3 兜底路径（磐石称拦停有「进编排前」与「跑完再兜」两条）：设计文本自带 HIGH 票、不注入
    hi_design = ctx.root / "examples/design-r1-high-ticket.md"
    if hi_design.exists():
        dbh = ctx.fresh_db("a3-fallback")
        ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(dbh)])
        evh = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(hi_design),
                       "--round", "3", "--dry-run", "--db", str(dbh),
                       "--workdir", str(ctx.tmp / "a3-fallback-work")])
        blobh = evh["stdout"] + evh["stderr"]
        c.add(cmd=evh["cmd"], rc=evh["rc"], stdout=evh["stdout"], stderr=evh["stderr"],
              ok=evh["rc"] == EXIT_CONFIRM,
              note="A3 兜底路径：设计文本自带未关闭 HIGH 票（不用 --inject）也必须拦停 → 退出码 3")
        if evh["rc"] != EXIT_CONFIRM:
            c.fail(f"A3：不带 --inject 的兜底拦停失效（rc={evh['rc']}，期望 3）—— "
                   "拦停不能只挂在 `--inject high_ticket` 这条通路上。")
        c.add(ok=cjk(blobh), note="A3 兜底路径：拒绝理由同样必须是中文")
    else:
        c.warn("树里没有 examples/design-r1-high-ticket.md，A3 的「不注入兜底」路径本轮未验。")


# ------------------------------------------------------------------ A4


def _complete_package(sc, round_no: int, *, with_prohibitions: bool = True):
    kw = dict(
        round_no=round_no, feature="轮次快照",
        goal="为每一轮生成可追溯的任务包快照",
        acceptance=[sc.AcceptanceCriterion(cid="AC-1", text="快照可查"),
                    sc.AcceptanceCriterion(cid="AC-2", text="package_hash 可复算")],
        boundaries=["空设计输入", "重复轮次号"],
        interface_contract=["Store.save_snapshot(snapshot) -> None"],
        prohibitions=["不得引入网络依赖"] if with_prohibitions else [],
        feedback=[])
    return sc.TaskPackage(**kw)


def check_a4(ctx: Ctx, c: Check) -> None:
    gates_mod, gerr = import_mod("kfpa.gates")
    store_mod, serr = import_mod("kfpa.store")
    if gates_mod is None or store_mod is None:
        c.add(ok=False, note=f"导入失败：gates={gerr} store={serr}")
        c.fail(missing_hint(ctx, "A4"))
        return
    sc = importlib.import_module("kfpa.schemas")
    if sc.CONTRACT_VERSION < "1.1":
        c.skip("契约 v1.0 无 results/error_category/reason_key，D1-3/D1-4 判定无法断言。")
        return

    def new_store(tag: str):
        st = make_store_for(store_mod, ctx.fresh_db(tag))
        try:
            st.init_schema()
        except Exception:  # noqa: BLE001
            pass
        return st

    # --- 纯函数：G1 缺段 / G2 执行失败 / G3 失败断言 / G4 断言未映射 / G6 PASS
    # 第 6 列 = D1-4 期望的 (category, target)，用于复算 reason_key；None = 只验态
    cases = [
        ("G1 缺段", 11, _complete_package(sc, 11, with_prohibitions=False),
         sc.ExecResult(ok=True, results={"AC-1": True, "AC-2": True}), "needs_fix", None),
        ("G2 执行失败", 12, _complete_package(sc, 12),
         sc.ExecResult(ok=False, error="mock 执行失败", error_category="apply_failed",
                       executor="mock"), "needs_fix", ("apply_failed", "round")),
        ("G3 失败断言", 13, _complete_package(sc, 13),
         sc.ExecResult(ok=True, failed_criteria=["AC-1"], results={"AC-1": True, "AC-2": True},
                       executor="mock"), "needs_fix", None),
        ("G4 断言未映射", 14, _complete_package(sc, 14),
         sc.ExecResult(ok=True, results={"AC-1": True}, executor="mock"),
         "needs_fix", ("unmapped_criterion", "ac-2")),
        ("G6 全过", 15, _complete_package(sc, 15),
         sc.ExecResult(ok=True, results={"AC-1": True, "AC-2": True}, executor="mock"), "pass", None),
    ]
    for label, rnd, pkg, ex, want, want_key in cases:
        st = new_store(f"a4-{rnd}")
        try:
            rpt = gates_mod.evaluate(rnd, pkg, ex, st, attempt=1)
            got = rpt.state.value
            c.add(ok=got == want, note=f"{label} → state={got}（期望 {want}）")
            if got != want:
                c.fail(f"D1-3 {label}：判定 {got} != {want}（reasons={rpt.reasons}）。")
            key = getattr(rpt, "reason_key", "") or ""
            if want_key:
                expect = spec_reason_key(*want_key)
                c.add(ok=key == expect,
                      note=f"{label} reason_key={key!r}（D1-4 复算 {want_key} → {expect!r}）")
                if key != expect:
                    c.fail(f"D1-4 {label}：reason_key 与规范复算不符（实际 {key!r}，"
                           f"期望 {expect!r}）—— 归一化键错会让同类失败计数打不中。")
            elif want == "needs_fix":
                c.add(ok=bool(re.fullmatch(r"[0-9a-f]{12}", key)),
                      note=f"{label} reason_key={key!r}（非 PASS 必须落 12 位归一化键）")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"{label} evaluate 异常：{type(exc).__name__}: {exc}")
            c.fail(f"D1-3：{label} 无法判定。")

    # --- G5 同类第 2 次失败即 BLOCKED（D1-4 + R-4）
    st = new_store("a4-g5")
    fail = sc.ExecResult(ok=False, error="mock 执行失败", error_category="test_fail",
                         failed_criteria=["AC-1"], executor="mock")
    seq = []
    try:
        for attempt in (1, 2, 3):
            rpt = gates_mod.evaluate(16, _complete_package(sc, 16), fail, st, attempt=attempt)
            seq.append({"attempt": attempt, "state": rpt.state.value,
                        "reason_key": getattr(rpt, "reason_key", ""),
                        "same_reason_count": getattr(rpt, "same_reason_count", None),
                        "escalated": getattr(rpt, "escalated", None)})
            if rpt.state is sc.GateState.BLOCKED:
                break
        c.add(ok=bool(seq), note=f"同因连续失败的态序 = {seq}")
        second = seq[1] if len(seq) > 1 else None
        ok2 = bool(second and second["state"] == "blocked" and second["escalated"] is True
                   and second["same_reason_count"] == 1)
        c.add(ok=ok2, note=f"D1-4/R-4：第 2 次 state={second and second['state']} "
                           f"escalated={second and second['escalated']} "
                           f"same_reason_count={second and second['same_reason_count']}")
        if not ok2:
            c.fail("D1-4/R-4：同一 reason_key 第 2 次失败未升为 BLOCKED + escalated=True + "
                   "same_reason_count=1（R-4：GateReport 记 count-1）。")
        ck = [s["reason_key"] for s in seq if s["reason_key"]]
        c.add(ok=bool(ck) and len(set(ck)) == 1, note=f"reason_key 稳定性 = {ck}")
        if ck and len(set(ck)) != 1:
            c.fail("D1-4：同一失败的 reason_key 不稳定，升级计数永远打不中。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"G5 evaluate 异常：{type(exc).__name__}: {exc}")
        c.fail("D1-4：同类失败升级路径不可判定。")

    # --- G5 前置：count_same_failure 口径（R-4：写入后调用，含本次）
    st = new_store("a4-count")
    try:
        for attempt in (1, 2):
            st.record_failure(sc.FailureRecord(round_no=17, attempt=attempt, reason="boom",
                                               avoid_pattern="AVOID[test_fail] x",
                                               reason_key="abc123"))
        n = st.count_same_failure(17, "abc123")
        c.add(ok=n == 2, note=f"R-4：record_failure 两次后 count_same_failure = {n}（期望 2）")
        if n != 2:
            c.fail(f"R-4：count_same_failure 口径不符（{n} != 2），门禁升级时点会漂。")
        pats = st.list_avoid_patterns(17)
        c.add(ok=bool(pats), note=f"回灌区 list_avoid_patterns(17) = {pats}")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"record_failure/count_same_failure 异常：{type(exc).__name__}: {exc}")
        c.fail("R-4：失败计数接口不可用（§3.2）。")

    # --- 端到端：--inject test_fail 必须产生 BLOCKED + 回灌区非空（D1-11）
    db = ctx.fresh_db("a4-e2e")
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    ev = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                  "--round", "1", "--dry-run", "--inject", "test_fail", "--json",
                  "--db", str(db), "--workdir", str(ctx.tmp / "a4-work")])
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] in (EXIT_NEEDS_FIX, EXIT_BLOCKED),
          note="A4/D1-5：--inject test_fail 期望 10(NEEDS_FIX) 或 11(BLOCKED)")
    if ev["rc"] not in (EXIT_NEEDS_FIX, EXIT_BLOCKED):
        c.fail(f"A4：--inject test_fail 退出码 {ev['rc']}，既不是 10 也不是 11。")
    try:
        st = make_store_for(store_mod, db)
        reports = st.list_gate_reports(1)
        states = [(r.attempt, r.state.value, getattr(r, "escalated", None),
                   getattr(r, "same_reason_count", None)) for r in reports]
        c.add(ok=bool(reports), note=f"落库的门禁报告（attempt,state,escalated,count）= {states}")
        if not reports:
            c.fail("A4：注入失败后 gate_reports 无记录（D1-5：不许静默重试）。")
        pkg = st.get_package(1)
        fb = list(getattr(pkg, "feedback", []) or []) if pkg else []
        c.add(ok=any("AVOID[" in str(x) for x in fb),
              note=f"D1-11：第 2 次任务包 feedback = {fb}")
        if not any("AVOID[" in str(x) for x in fb):
            c.fail("D1-11（P0 必做）：失败未回灌 —— 任务包 feedback 段里没有 `AVOID[...]` 行。")
        pats = st.list_avoid_patterns(1)
        c.add(ok=bool(pats), note=f"failures.avoid_pattern = {pats}")
        if not pats:
            c.fail("D1-4：failures 表未写 avoid_pattern（负知识无落点）。")
        blocked = [r for r in reports if r.state.value == "blocked"]
        if blocked:
            last = blocked[-1]
            c.add(ok=bool(getattr(last, "escalated", False)),
                  note=f"BLOCKED 报告 escalated={getattr(last, 'escalated', None)}")
            if not getattr(last, "escalated", False):
                c.fail("D1-4：BLOCKED 未带 escalated=True。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"端到端读取异常：{type(exc).__name__}: {exc}")
        c.fail("A4：端到端结果不可读。")


# ------------------------------------------------------------------ A5


def check_a5_business_scan(ctx: Ctx, c: Check) -> None:
    """R-11.1：业务代码零模型串（排除唯一白名单文件 src/kfpa/llm.py）。"""
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
    c.add(ok=not bad, note=f"R-11.1：src/ 命中 {len(hits)} 处模型字符串，越界 {len(bad)} 处；"
                           f"白名单 = {list(MODEL_STR_ALLOWED)}")
    for rel, i, line in hits[:20]:
        c.findings.append(f"[{'越界' if rel not in MODEL_STR_ALLOWED else '白名单'}] "
                          f"{rel}:{i}: {line[:120]}")
    if not hits:
        c.findings.append("src/ 全库无模型字符串命中（铁律成立）。")
    if bad:
        c.fail("R-11.1/A5.1 铁律被击穿：src/ 业务代码里出现模型字符串 —— "
               + "; ".join(f"{r}:{i}" for r, i, _ in bad)
               + "。白名单只有一个文件：src/kfpa/llm.py（新增白名单须经 Chief 裁决）。")


def check_a5_literal(ctx: Ctx, c: Check) -> None:
    """R-11.2：白名单文件自身也不许有字面量模型串（只能从 config/models.yaml 读）。"""
    llm = ctx.root / "src/kfpa/llm.py"
    if not llm.exists():
        c.fail("缺 src/kfpa/llm.py，R-11.2 无法验收。")
        return
    pat = re.compile(r"[\"'](?:deepseek|gpt|claude|qwen|bge)[^\"']*[\"']", re.I)
    hits = [(i, line.strip()) for i, line in
            enumerate(llm.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
            if pat.search(line)]
    c.add(ok=not hits, note=f"R-11.2：src/kfpa/llm.py 字面量模型串命中 {len(hits)} 处（期望 0）")
    for i, line in hits[:10]:
        c.findings.append(f"[越界] src/kfpa/llm.py:{i}: {line[:140]}")
    if hits:
        c.fail("R-11.2：适配器里出现字面量模型串 —— 模型名必须只来自 config/models.yaml。")


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
    out["roles_error"] = "%s: %s" % (type(e).__name__, e); roles = {}
try:
    llm = m.build_llm(roles, provider="mock")
    prompts = {
        "d1_marker": "[KFPA:GEN_TICKETS]\nQA: design.md \u00a75.1 | A: \u7532 | B: \u4e59 | P: \u7532 | I: MEDIUM",
        "r3_marker": "KFPA_TASK=qa_tickets\nQA: design.md \u00a75.1 | A: \u7532 | B: \u4e59 | P: \u7532 | I: MEDIUM",
        "keyword": "\u4ee5\u4e0b\u8bbe\u8ba1\u5b58\u5728\u6b67\u4e49\uff0c\u8bf7\u4ea7\u51fa\u8cea\u554f\u7968\uff1a\nQA: design.md \u00a75.1 | A: \u7532 | B: \u4e59 | P: \u7532 | I: MEDIUM",
    }
    errs = {}
    seen = {}
    for kind, prompt in prompts.items():
        try:
            a = llm.complete("ARCHITECT", "sys", prompt)
            b = llm.complete("ARCHITECT", "sys", prompt)
            errs[kind] = None
            seen[kind] = {"deterministic": (a == b), "sample": str(a)[:120], "raw": a}
        except Exception as e:
            errs[kind] = "%s: %s" % (type(e).__name__, e)
    out["prompt_errors"] = {k: v for k, v in errs.items() if v}
    out["prompt_ok"] = [k for k, v in errs.items() if not v]
    out["mock_prompt_kind"] = ",".join(out["prompt_ok"])
    out["mock_call_ok"] = bool(out["prompt_ok"])
    if "d1_marker" in seen:
        out["mock_deterministic"] = seen["d1_marker"]["deterministic"]
        out["mock_returns_str"] = True
        out["mock_sample"] = seen["d1_marker"]["sample"]
    out["alias_same_output"] = (
        "d1_marker" in seen and "r3_marker" in seen
        and seen["d1_marker"]["raw"] == seen["r3_marker"]["raw"])
except Exception as e:
    out["mock_error"] = "%s: %s" % (type(e).__name__, e)
try:
    m.MockLLM().complete("BOGUS", "sys",
                         "[KFPA:GEN_TICKETS]\nQA: x | A: a | B: b | P: a | I: LOW")
    out["bogus_role_rejected"] = False
except Exception as e:
    out["bogus_role_rejected"] = True
    out["bogus_role_exc"] = type(e).__name__
try:
    hi = m.build_llm(roles, provider="mock", high_impact=True)
    out["high_impact_kwarg"] = hasattr(hi, "complete")
except Exception as e:
    out["high_impact_error"] = "%s: %s" % (type(e).__name__, e)
print(json.dumps(out, ensure_ascii=False))
'''
    ev = ctx.run([ctx.python, "-c", probe])
    c.add(cmd="python -c <A5 适配器探针>", rc=ev["rc"], stdout=ev["stdout"],
          stderr=ev["stderr"], ok=ev["rc"] == 0,
          note="导入 kfpa.llm，检查惰性导入/角色/Mock 确定性/high_impact 开关")
    if ev["rc"] != 0:
        c.fail(f"适配器探针自身失败：rc={ev['rc']} {excerpt(ev['stderr'], 300)}")
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
    c.add(ok=bool(data.get("has_MockLLM")), note="MockLLM 存在（离线验收路径）")
    c.add(ok=bool(data.get("has_LiteLLMAdapter")), note="LiteLLMAdapter 存在（可选适配器）")
    if data.get("roles_error"):
        c.add(ok=False, note=f"load_roles 异常：{data['roles_error']}")
        c.fail("§3.3 load_roles 不可用（应读 config/models.yaml）。")
        return
    want = {"ARCHITECT", "REVIEWER", "FLASH", "EMBEDDING"}
    got = set(data.get("roles") or [])
    c.add(ok=got == want, note=f"角色集合 = {sorted(got)}（期望 {sorted(want)}）")
    if got != want:
        c.fail(f"§3.3 角色集合不符：多 = {sorted(got - want)}，缺 = {sorted(want - got)}。")
    c.add(ok=bool(data.get("roles_have_model")), note="每个角色都有 model 字段（唯一绑定点在 config）")
    c.add(ok=bool(data.get("mock_call_ok")),
          note=f"MockLLM.complete 可用 = {data.get('mock_call_ok')}"
               f"（可用的 prompt 口径 = {data.get('mock_prompt_kind')}）")
    if data.get("mock_error"):
        c.add(ok=False, note=f"MockLLM 调用异常：{data['mock_error']}")
        c.fail("§3.3/D1-7：MockLLM 无法取得剧本输出。")
    # R-17：规范 marker（KFPA_TASK=）与永久兼容别名（[KFPA:…]）都必须可用，且输出一致
    ok_kinds = set(data.get("prompt_ok") or [])
    for kind, label, ref in (("r3_marker", "规范 marker `KFPA_TASK=qa_tickets`", "R-17/§3.3"),
                             ("d1_marker", "永久兼容别名 `[KFPA:GEN_TICKETS]`", "R-17/§3.3"),
                             ("keyword", "关键词兜底（无 marker）", "R-3")):
        ok = kind in ok_kinds
        c.add(ok=ok, note=f"{ref}：{label} 可用 = {ok}"
                          + ("" if ok else f"；错误 = {(data.get('prompt_errors') or {}).get(kind)}"))
        if not ok:
            c.fail(f"{ref}：{label} 不可用 —— "
                   f"{(data.get('prompt_errors') or {}).get(kind)}")
    c.add(ok=bool(data.get("alias_same_output")),
          note=f"R-17：规范 marker 与别名输出一致 = {data.get('alias_same_output')}")
    if not data.get("alias_same_output"):
        c.fail("R-17：`KFPA_TASK=` 与 `[KFPA:…]` 两种 marker 的输出不一致（同义别名必须等价）。")
    c.add(ok=bool(data.get("mock_deterministic")),
          note=f"D1-7：MockLLM 同输入同输出 = {data.get('mock_deterministic')}")
    if data.get("mock_call_ok") and data.get("mock_deterministic") is False:
        c.fail("D1-7：MockLLM 不确定性 —— 同一 role/system/user 两次输出不同，快照不可复算。")
    c.add(ok=bool(data.get("bogus_role_rejected")),
          note=f"非法 role('BOGUS') 被拒 = {data.get('bogus_role_rejected')} "
               f"({data.get('bogus_role_exc')})")
    if not data.get("bogus_role_rejected"):
        c.fail("§3.3：非法 role 未被拒绝（只接受 ARCHITECT/REVIEWER/FLASH/EMBEDDING）。")
    c.add(ok=bool(data.get("high_impact_kwarg")),
          note=f"R-3：build_llm(..., high_impact=True) 可用 = "
               f"{bool(data.get('high_impact_kwarg'))} ({data.get('high_impact_error', '')})")
    if not data.get("high_impact_kwarg"):
        c.fail("R-3：`build_llm(roles, provider='mock', high_impact=True)` 不可用，"
               "A3 无法用 mock 造高影响票。")
    c.warn("R-2：p0-plan §4 的示例命令仍写 `python -m kfpa …`；统一口径应为 "
           "`PYTHONPATH=src python -m kfpa …`（本台架已按 R-2 注入 PYTHONPATH）。")


# ------------------------------------------------------------------ A6


def check_a6(ctx: Ctx, c: Check) -> None:
    db = ctx.fresh_db("a6")
    work = ctx.tmp / "a6-work"
    work.mkdir(parents=True, exist_ok=True)
    ev1 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev1["cmd"], rc=ev1["rc"], ok=ev1["rc"] == EXIT_PASS, note="A6 前置：建库")
    ev2 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "1", "--dry-run", "--json", "--db", str(db),
                   "--workdir", str(work)])
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] == EXIT_PASS, note="A6 前置：整轮（只有 PASS 轮才打 tag，D1-6）")
    if ev2["rc"] != EXIT_PASS:
        c.fail("整轮未 PASS，A6 无法从真实轮次验收快照 —— 先修 A2/A4。")

    snap = {}
    try:
        store_mod = importlib.import_module("kfpa.store")
        st = make_store_for(store_mod, db)
        snaps = st.list_snapshots()
        c.add(ok=len(snaps) >= 1, note=f"Store.list_snapshots() 返回 {len(snaps)} 条")
        if snaps:
            snap = snaps[-1]
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"list_snapshots 异常：{type(exc).__name__}: {exc}")
        c.fail(f"§3.2 Store.list_snapshots 不可用（{OWNERS['src/kfpa/store.py']}）。")

    tables, counts, cols, err = probe_db(db)
    n = counts.get("snapshots", -1)
    c.add(ok=n >= 1, note=f"原始库 snapshots 行数 = {n}" + (f"；读取错误 {err}" if err else ""))
    if n < 1:
        c.fail("D1-6/A6：PASS 轮必须落 snapshots，实际 0 行。")

    ev3 = ctx.run([ctx.python, "-m", "kfpa", "history", "--db", str(db)])
    blob = ev3["stdout"] + ev3["stderr"]
    c.add(cmd=ev3["cmd"], rc=ev3["rc"], stdout=ev3["stdout"], stderr=ev3["stderr"],
          ok=ev3["rc"] == EXIT_PASS and bool(blob.strip()),
          note="D1-6：`kfpa history` 列 round_no|state|package_hash|gate_state|tag（期望 0）")
    if ev3["rc"] != EXIT_PASS or not blob.strip():
        c.fail("D1-6/A6：`kfpa history` 不可用或输出为空。")
        return
    ph = str(getattr(snap, "package_hash", "") or "")
    tag = str(getattr(snap, "tag", "") or "")
    if ph:
        c.add(ok=ph[:8] in blob, note=f"history 含 package_hash 前 8 位 {ph[:8]}",
              stdout=excerpt(blob, 400))
        if ph[:8] not in blob:
            c.fail(f"D1-6：history 未列出 package_hash（{ph[:8]} 不在输出里）。")
    want_tag = "kfpa-r1"
    c.add(ok=tag == want_tag, note=f"D1-6：快照 tag = {tag!r}（期望 {want_tag!r}）")
    if tag != want_tag:
        c.fail(f"D1-6：PASS 轮 tag 应为 `{want_tag}`，实际 `{tag}`。")
    elif want_tag not in blob:
        c.fail(f"D1-6：history 未列出 tag `{want_tag}`。")
    rounds = counts.get("rounds", 0)
    c.add(ok=n <= max(rounds, 1),
          note=f"snapshots 行数 {n} <= rounds 行数 {rounds}（只有 PASS 才落快照）")


# ------------------------------------------------------------------ A7


def _ensure_tickets(ctx: Ctx, db: Path, tag: str) -> tuple[list[str], str]:
    rt = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    if rt["rc"] != EXIT_PASS:
        return [], f"init 失败({rt['rc']})：{excerpt(rt['stderr'] or rt['stdout'], 200)}"
    work = ctx.tmp / f"{tag}-work"
    work.mkdir(parents=True, exist_ok=True)
    ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
             "--round", "1", "--dry-run", "--db", str(db), "--workdir", str(work)])
    try:
        store_mod = importlib.import_module("kfpa.store")
    except Exception as exc:  # noqa: BLE001
        return [], f"import kfpa.store 失败：{type(exc).__name__}: {exc}"
    st = make_store_for(store_mod, db)
    ids = []
    try:
        ids = [t.ticket_id for t in st.list_tickets()]
    except Exception:  # noqa: BLE001
        pass
    if not ids:
        try:
            seed_ticket(store_mod, db, ticket_id="R1-901", round_no=1, impact="MEDIUM")
            seed_ticket(store_mod, db, ticket_id="R1-902", round_no=1, impact="HIGH")
            ids = ["R1-901", "R1-902"]
        except Exception as exc:  # noqa: BLE001
            return [], f"造票失败（Store.add_ticket）：{type(exc).__name__}: {exc}"
    return ids, ""


def check_a7(ctx: Ctx, c: Check) -> None:
    db = ctx.fresh_db("a7")
    ids, err = _ensure_tickets(ctx, db, "a7")
    if err:
        c.add(ok=False, note=err)
        c.fail("A7 无法准备票数据：" + err)
        return
    tid = ids[0]
    c.add(ok=True, note=f"用例票 = {ids}")

    ev = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--db", str(db)])
    blob = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS, note="D1-8：台账可读（期望 0）")
    if ev["rc"] != EXIT_PASS:
        c.fail(f"`kfpa tickets list` 不可用（{OWNERS['src/kfpa/ui/cli.py']}）。")
        return
    c.add(ok=tid in blob, note=f"列表含票号 {tid}")
    if tid not in blob:
        c.fail(f"D1-8/A7：台账列表未列出票号 {tid}。")
    # R-18：这一遍不设 PYTHONIOENCODING / PYTHONUTF8
    evq = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--db", str(db)],
                  clean_env=True)
    blobq = evq["stdout"] + evq["stderr"]
    c.add(cmd=evq["cmd"] + "    [clean-env: 未设 PYTHONIOENCODING/PYTHONUTF8]",
          rc=evq["rc"], stdout=evq["stdout"], stderr=evq["stderr"],
          ok=evq["rc"] == EXIT_PASS and "UnicodeEncodeError" not in blobq and tid in blobq,
          note="R-18/R-18.4：台账在重定向且不设 PYTHONIOENCODING 时也要能正常输出中文")
    if evq["rc"] != EXIT_PASS or "UnicodeEncodeError" in blobq or tid not in blobq:
        c.fail(f"R-18：不设 PYTHONIOENCODING 时 `tickets list` rc={evq['rc']}、"
               f"含票号={tid in blobq}"
               f"{'（UnicodeEncodeError）' if 'UnicodeEncodeError' in blobq else ''}。")
    low = blob.lower()
    for label, keys in (("影响面", ("high", "medium", "low", "高", "中", "低")),
                        ("状态", ("open", "closed", "dropped", "未关闭", "已关闭")),
                        ("位置", ("design", "§", "location", "位置")),
                        ("倾向", ("倾向", "preference", "a：", "b：", "解释"))):
        ok = any(k in low for k in keys)
        c.add(ok=ok, note=f"D1-8：台账含{label}列", stdout=excerpt(blob, 400))
        if not ok:
            c.fail(f"D1-8：`tickets list` 输出缺少{label}列。")

    for flag, value in (("--round", "1"), ("--impact", "HIGH"), ("--status", "OPEN")):
        e = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", flag, value, "--db", str(db)])
        c.add(cmd=e["cmd"], rc=e["rc"], stdout=e["stdout"], stderr=e["stderr"],
              ok=e["rc"] == EXIT_PASS, note=f"过滤器 {flag} {value} 可用（§3.7）")
        if e["rc"] != EXIT_PASS:
            c.fail(f"§3.7：`tickets list {flag} {value}` 退出码 {e['rc']}（期望 0）。")

    e = ctx.run([ctx.python, "-m", "kfpa", "tickets", "close", tid,
                 "--answer", "选 A：按仓隔离", "--db", str(db)])
    c.add(cmd=e["cmd"], rc=e["rc"], stdout=e["stdout"], stderr=e["stderr"],
          ok=e["rc"] == EXIT_PASS, note="D1-8：关票并入库（期望 0）")
    if e["rc"] != EXIT_PASS:
        c.fail("D1-8/A7：`tickets close` 不可用。")
        return
    try:
        con = sqlite3.connect(str(db))
        row = con.execute("select status, answer from tickets where ticket_id = ?",
                          (tid,)).fetchone()
        con.close()
        c.add(ok=bool(row) and str(row[0]).lower().startswith("clos"),
              note=f"原始库 tickets 行 = {row}")
        if not row or not str(row[0]).lower().startswith("clos"):
            c.fail(f"D1-8/A7：关票未落库（{tid} 状态 = {row[0] if row else '缺行'}）。")
        if row and not (row[1] or "").strip():
            c.fail("D1-8/A7：人工确认结论（answer）未落库。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"直读 tickets 表失败：{type(exc).__name__}: {exc}")
        c.fail("tickets 表不可直读。")

    e2 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--status", "OPEN", "--db", str(db)])
    blob2 = e2["stdout"] + e2["stderr"]
    c.add(cmd=e2["cmd"], rc=e2["rc"], ok=e2["rc"] == EXIT_PASS and tid not in blob2,
          note=f"关闭后 `--status OPEN` 不再列出 {tid}", stdout=excerpt(blob2, 300))
    if tid in blob2:
        c.fail("D1-8/A7：已关闭的票仍出现在 `--status OPEN` 列表里。")

    # batch-close（D1-8）：映射格式 + 未知票号整体拒绝（事务）
    db2 = ctx.fresh_db("a7b")
    ids2, err2 = _ensure_tickets(ctx, db2, "a7b")
    if err2:
        c.fail("A7 batch-close 前置失败：" + err2)
        return
    y = ctx.tmp / "answers.yaml"
    y.write_text("".join(f'{i}: "采用 A：按仓隔离"\n' for i in ids2), encoding="utf-8")
    eb = ctx.run([ctx.python, "-m", "kfpa", "tickets", "batch-close", "--file", str(y),
                  "--db", str(db2)])
    c.add(cmd=eb["cmd"], rc=eb["rc"], stdout=eb["stdout"], stderr=eb["stderr"],
          ok=eb["rc"] == EXIT_PASS, note="D1-8：`票号 -> 结论` 映射攒批确认（期望 0）")
    if eb["rc"] != EXIT_PASS:
        c.fail("D1-8：batch-close 未接受「票号 -> 结论」映射格式。")
    else:
        try:
            con = sqlite3.connect(str(db2))
            closed = con.execute("select count(*) from tickets where status='closed'").fetchone()[0]
            con.close()
            c.add(ok=closed >= 1, note=f"batch-close 后 closed 票数 = {closed}")
            if closed < 1:
                c.fail("D1-8：batch-close 报成功但没有任何票被关闭。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"直读失败：{exc}")

    db3 = ctx.fresh_db("a7c")
    ids3, err3 = _ensure_tickets(ctx, db3, "a7c")
    if err3:
        c.fail("A7 事务用例前置失败：" + err3)
        return
    bad = ctx.tmp / "answers_bad.yaml"
    bad.write_text(f'{ids3[0]}: "采用 A"\nR9-999: "不存在的票"\n', encoding="utf-8")
    ex = ctx.run([ctx.python, "-m", "kfpa", "tickets", "batch-close", "--file", str(bad),
                  "--db", str(db3)])
    c.add(cmd=ex["cmd"], rc=ex["rc"], stdout=ex["stdout"], stderr=ex["stderr"],
          ok=ex["rc"] == EXIT_USAGE, note="D1-8：未知票号 → 退出码 2（用法错误）")
    if ex["rc"] != EXIT_USAGE:
        c.fail(f"D1-8：未知票号应退出码 2，实际 {ex['rc']}。")
    try:
        con = sqlite3.connect(str(db3))
        n_closed = con.execute("select count(*) from tickets where status='closed'").fetchone()[0]
        con.close()
        c.add(ok=n_closed == 0,
              note=f"D1-8：未知票号时整体不写 —— closed 票数 = {n_closed}（期望 0）")
        if n_closed != 0:
            c.fail(f"D1-8 事务被击穿：未知票号时仍写入了 {n_closed} 张已关闭票。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"直读失败：{exc}")


# ------------------------------------------------------------------ A7.2


def check_a7_rules(ctx: Ctx, c: Check) -> None:
    """R-8（impact 下推 Store）+ R-9（batch-close 事务与幂等）。"""
    store_mod, serr = import_mod("kfpa.store")
    if store_mod is None:
        c.add(ok=False, note=f"import kfpa.store 失败：{serr}")
        c.fail("R-8 无法验收：kfpa.store 不可导入。")
        return
    sc = importlib.import_module("kfpa.schemas")

    # --- R-8：list_tickets 必须收 impact，且下推到 SQL（不是界面本地过滤）
    try:
        params = list(inspect.signature(store_mod.Store.list_tickets).parameters)
        c.add(ok="impact" in params, note=f"R-8：Store.list_tickets 参数 = {params}")
        if "impact" not in params:
            c.fail("R-8：`Store.list_tickets()` 没有 `impact` 参数 —— 界面按 R-8 传参会抛 "
                   "TypeError，`kfpa tickets list`（含不带 --impact 的普通调用）会整体失败。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"取签名失败：{type(exc).__name__}: {exc}")

    db = ctx.fresh_db("a72")
    ev0 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev0["cmd"], rc=ev0["rc"], ok=ev0["rc"] == EXIT_PASS, note="A7.2 前置：建库")
    try:
        seed_ticket(store_mod, db, ticket_id="R1-911", round_no=1, impact="MEDIUM")
        seed_ticket(store_mod, db, ticket_id="R1-912", round_no=1, impact="HIGH")
        st = make_store_for(store_mod, db)
        got = st.list_tickets(impact=sc.TicketImpact.HIGH)
        ok = [t.ticket_id for t in got] == ["R1-912"]
        c.add(ok=ok, note=f"R-8：list_tickets(impact=HIGH) 返回 {[t.ticket_id for t in got]}"
                          f"（期望 ['R1-912']）")
        if not ok:
            c.fail("R-8：Store 侧 impact 过滤不正确（应只返回 HIGH 票）。")
    except TypeError as exc:
        c.add(ok=False, note=f"R-8：调用 list_tickets(impact=...) 抛 TypeError: {exc}")
        c.fail("R-8：Store.list_tickets 未实现 impact 过滤。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"R-8 用例异常：{type(exc).__name__}: {exc}")

    # --- R-9：重复提交（已关闭票）幂等跳过 → 0；空答案 → 2 且整体不写
    db2 = ctx.fresh_db("a72b")
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db2)])
    try:
        seed_ticket(store_mod, db2, ticket_id="R1-921", round_no=1, impact="MEDIUM")
        seed_ticket(store_mod, db2, ticket_id="R1-922", round_no=1, impact="LOW")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"造票失败：{type(exc).__name__}: {exc}")
        return
    f1 = ctx.tmp / "r9-ok.yaml"
    f1.write_text('R1-921: "采用 A"\nR1-922: "采用 B"\n', encoding="utf-8")
    e1 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "batch-close", "--file", str(f1),
                  "--db", str(db2)])
    c.add(cmd=e1["cmd"], rc=e1["rc"], ok=e1["rc"] == EXIT_PASS,
          note="R-9：正常攒批期望 0", stdout=excerpt(e1["stdout"] + e1["stderr"], 300))
    e2 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "batch-close", "--file", str(f1),
                  "--db", str(db2)])
    blob2 = e2["stdout"] + e2["stderr"]
    c.add(cmd=e2["cmd"], rc=e2["rc"], ok=e2["rc"] == EXIT_PASS,
          note="R-9：已关闭票重复提交 → 幂等跳过，期望 0（禁止用 1）",
          stdout=excerpt(blob2, 400))
    if e2["rc"] != EXIT_PASS:
        c.fail(f"R-9：已关闭票重复提交退出码 {e2['rc']}，期望 0（R-1 已作废 1）。")
    c.add(ok=("跳过" in blob2 or "skip" in blob2.lower()),
          note="R-9：重复提交要打印「跳过 + 原因」")

    # --- R-15：单张 close 遇已关闭票 → 幂等 0；batch-close 文件不存在 → 4
    e3 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "close", "R1-921",
                  "--answer", "再次确认：采用 A", "--db", str(db2)])
    blob3 = e3["stdout"] + e3["stderr"]
    c.add(cmd=e3["cmd"], rc=e3["rc"], ok=e3["rc"] == EXIT_PASS,
          note="R-15：单张 close 撞已关闭票 → 幂等忽略 + 退出码 0",
          stdout=excerpt(blob3, 300))
    if e3["rc"] != EXIT_PASS:
        c.fail(f"R-15：单张 `tickets close` 撞已关闭票退出码 {e3['rc']}，期望 0（幂等）。")

    e4 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "batch-close",
                  "--file", str(ctx.tmp / "no-such-answers.yaml"), "--db", str(db2)])
    c.add(cmd=e4["cmd"], rc=e4["rc"], ok=e4["rc"] == EXIT_DEP,
          note="R-15：batch-close 答案文件不存在 → 退出码 4（依赖/环境错误）")
    if e4["rc"] != EXIT_DEP:
        c.fail(f"R-15：答案文件不存在应退出码 4，实际 {e4['rc']}。")

    db3 = ctx.fresh_db("a72c")
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db3)])
    try:
        seed_ticket(store_mod, db3, ticket_id="R1-931", round_no=1, impact="MEDIUM")
        seed_ticket(store_mod, db3, ticket_id="R1-932", round_no=1, impact="LOW")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"造票失败：{type(exc).__name__}: {exc}")
        return
    f2 = ctx.tmp / "r9-empty.yaml"
    f2.write_text('R1-931: "采用 A"\nR1-932: ""\n', encoding="utf-8")
    e3 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "batch-close", "--file", str(f2),
                  "--db", str(db3)])
    c.add(cmd=e3["cmd"], rc=e3["rc"], stdout=e3["stdout"], stderr=e3["stderr"],
          ok=e3["rc"] == EXIT_USAGE, note="R-9：答案为空 → 退出码 2（输入错误）")
    if e3["rc"] != EXIT_USAGE:
        c.fail(f"R-9：含空答案应退出码 2，实际 {e3['rc']}。")
    try:
        con = sqlite3.connect(str(db3))
        n_closed = con.execute("select count(*) from tickets where status='closed'").fetchone()[0]
        con.close()
        c.add(ok=n_closed == 0, note=f"R-9：空答案时整体不写 —— closed = {n_closed}（期望 0）")
        if n_closed != 0:
            c.fail(f"R-9 事务被击穿：含空答案时仍写了 {n_closed} 张已关闭票。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"直读失败：{exc}")


# ------------------------------------------------------------------ A8


def check_a8(ctx: Ctx, c: Check) -> None:
    smoke = ctx.root / "tests/test_graph_smoke.py"
    if not smoke.exists():
        c.fail("缺 tests/test_graph_smoke.py（镜鉴自己的交付物 = A8 的验收命令）。")
        return
    ev = ctx.run([ctx.python, "-m", "pytest", "tests/test_graph_smoke.py", "-q"])
    blob = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == 0 and "skipped" not in blob.lower(),
          note="A8：`python -m pytest tests/test_graph_smoke.py`（D1-12 约束 3）")
    if ev["rc"] != 0:
        c.fail(f"A8：图冒烟未通过（pytest rc={ev['rc']}）。")
    elif "skipped" in blob.lower():
        c.fail("A8：图冒烟被 skip（build_graph 未交付或不可调用）—— skip 不等于通过。")
    else:
        c.add(ok=True, note="图可编译并可跑通一次 dry-run", stdout=excerpt(blob, 300))
    pipe_mod, perr = import_mod("kfpa.pipeline")
    if pipe_mod is None:
        c.add(ok=False, note=f"import kfpa.pipeline 失败：{perr}")
        c.fail("D1-12：pipeline 不可导入，纯函数核心无保险丝。")
    else:
        ok = hasattr(pipe_mod, "build_graph")
        c.add(ok=ok, note="pipeline.build_graph 存在（图坏掉时可降级为可选编排层）")
        if not ok:
            c.fail("D1-12：`pipeline.build_graph()` 不存在。")


# ------------------------------------------------------------------ A9


def check_a9(ctx: Ctx, c: Check) -> None:
    probe = r'''
import json, sys
out = {"patched": []}
try:
    import kfpa.llm as m
except Exception as e:
    print(json.dumps({"import_error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False)); raise SystemExit(0)
try:
    import litellm
except Exception as e:
    print(json.dumps({"litellm_missing": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False)); raise SystemExit(0)
roles = m.load_roles()
for name, spec in roles.items():
    if isinstance(spec, dict):
        spec["model"] = "nonexistent/fake-model-for-a9"
adapter = None
for how in ("build_llm", "LiteLLMAdapter"):
    try:
        if how == "build_llm":
            adapter = m.build_llm(roles, provider="litellm", api_key="")
        else:
            try:
                adapter = m.LiteLLMAdapter(roles, api_key="")
            except TypeError:
                adapter = m.LiteLLMAdapter(roles)
        out["built_by"] = how
        break
    except Exception as e:
        out.setdefault("build_errors", []).append("%s: %s: %s" % (how, type(e).__name__, e))

def boom(*a, **k):
    raise ValueError("simulated-network-boom-a9")

mod = sys.modules.get("litellm")
for name in ("completion", "acompletion", "text_completion"):
    if mod is not None and hasattr(mod, name):
        setattr(mod, name, boom); out["patched"].append("litellm." + name)
if adapter is not None:
    amod = sys.modules.get(type(adapter).__module__)
    for name in ("completion", "acompletion"):
        if amod is not None and hasattr(amod, name):
            setattr(amod, name, boom); out["patched"].append(amod.__name__ + "." + name)

out["adapter_type"] = type(adapter).__name__ if adapter is not None else None
if adapter is None:
    print(json.dumps(out, ensure_ascii=False)); raise SystemExit(0)
try:
    adapter.complete("FLASH", "sys", "usr")
    out["raised"] = None
except BaseException as e:
    out["raised"] = type(e).__name__
    out["message"] = str(e)[:400]
print(json.dumps(out, ensure_ascii=False))
'''
    ev = ctx.run([ctx.python, "-c", probe], timeout=120)
    c.add(cmd="python -c <A9 适配器异常包装探针>", rc=ev["rc"], stdout=ev["stdout"],
          stderr=ev["stderr"], ok=ev["rc"] == 0, note="R-7：假模型名 + 空 key + 注入底层异常")
    if ev["rc"] != 0:
        c.fail(f"A9 探针自身失败：rc={ev['rc']} {excerpt(ev['stderr'], 400)}")
        return
    try:
        data = json.loads(ev["stdout"].strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"探针输出不是 JSON：{exc}")
        c.fail("A9 无法解析探针输出。")
        return
    if data.get("import_error"):
        c.add(ok=False, note=f"import kfpa.llm 失败：{data['import_error']}")
        c.fail("A9：kfpa.llm 不可导入。")
        return
    if data.get("litellm_missing"):
        c.skip(f"本机 litellm 不可导入（{data['litellm_missing']}），A9 无法断言包装逻辑。")
        return
    if data.get("adapter_type") is None:
        c.add(ok=False, note=f"适配器构造失败：{data.get('build_errors')}")
        c.fail("A9：LiteLLMAdapter 无法构造（§3.3），异常包装无从验收。")
        return
    c.add(ok=True, note=f"适配器 = {data['adapter_type']}；已拦截调用点 = {data.get('patched')}")
    if not data.get("patched"):
        c.fail("A9：无法拦截任何底层调用点（litellm.completion 等），包装逻辑不可断言；"
               "建议把 litellm 调用收敛到一个可注入的方法上。")
        return
    raised, msg = data.get("raised"), data.get("message") or ""
    c.add(ok=raised == "RuntimeError",
          note=f"R-7①：底层抛 ValueError → 适配器抛 {raised}（期望 RuntimeError，不许裸抛）")
    if raised != "RuntimeError":
        c.fail(f"R-7①：适配器未把底层异常统一包成 RuntimeError（实际 {raised}）。")
    c.add(ok=cjk(msg), note=f"R-7②：消息含中文 = {cjk(msg)}；片段 = {msg[:160]!r}")
    if not cjk(msg):
        c.fail("R-7②：包装后的异常消息没有中文提示。")
    ok_hint = ("--provider" in msg and "mock" in msg)
    c.add(ok=ok_hint, note="R-7②：消息提示改用 `--provider mock`")
    if not ok_hint:
        c.fail("R-7②：包装后的异常消息未提示 `--provider mock`。")


# ------------------------------------------------------------------ A10


def check_a10(ctx: Ctx, c: Check) -> None:
    """R-18：stdout 被重定向（非 UTF-8 locale）且**不设** PYTHONIOENCODING / PYTHONUTF8 时，
    `--help` / `init` / `tickets list` / `round --json` 都不得编码崩溃，rc 符合 D1-5。"""
    print(f"   [A10] 台架自身 env：PYTHONIOENCODING={os.environ.get('PYTHONIOENCODING')!r}、"
          f"PYTHONUTF8={os.environ.get('PYTHONUTF8')!r}（子进程这一遍两个都不传）")

    db = ctx.fresh_db("a10")
    # ① --help
    ev = ctx.run([ctx.python, "-m", "kfpa", "--help"], clean_env=True)
    blob = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"] + "    [clean-env]", rc=ev["rc"], stdout=ev["stdout"],
          stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS and "UnicodeEncodeError" not in blob,
          note="R-18：--help 期望 rc=0（D1-5）且无 UnicodeEncodeError")
    if ev["rc"] != EXIT_PASS or "UnicodeEncodeError" in blob:
        c.fail(f"R-18：`--help` rc={ev['rc']}，输出含 UnicodeEncodeError="
               f"{'UnicodeEncodeError' in blob}。")
    c.add(ok=cjk(ev["stdout"]),
          note="R-18：stdout 能按 UTF-8 解出中文（说明兜底 reconfigure 生效，而不是 errors=replace 掩盖）")
    if not cjk(ev["stdout"]):
        c.fail("R-18：`--help` 的 stdout 解不出中文 —— 兜底没写，或被 errors=replace 掩盖成乱码。")

    # ② init
    ev2 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)], clean_env=True)
    blob2 = ev2["stdout"] + ev2["stderr"]
    c.add(cmd=ev2["cmd"] + "    [clean-env]", rc=ev2["rc"], stdout=ev2["stdout"],
          stderr=ev2["stderr"],
          ok=ev2["rc"] == EXIT_PASS and "UnicodeEncodeError" not in blob2,
          note="R-18：init 期望 rc=0（D1-5）")

    # ③ tickets list（先造票，保证有中文输出）
    store_mod, serr = import_mod("kfpa.store")
    if store_mod is None:
        c.add(ok=False, note=f"import kfpa.store 失败：{serr}")
        c.fail("A10：无法准备票数据。")
        return
    try:
        seed_ticket(store_mod, db, ticket_id="R1-941", round_no=1, impact="MEDIUM")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"造票失败：{type(exc).__name__}: {exc}")
        c.fail("A10：无法准备票数据。")
        return
    ev3 = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--db", str(db)], clean_env=True)
    blob3 = ev3["stdout"] + ev3["stderr"]
    c.add(cmd=ev3["cmd"] + "    [clean-env]", rc=ev3["rc"], stdout=ev3["stdout"],
          stderr=ev3["stderr"],
          ok=ev3["rc"] == EXIT_PASS and "UnicodeEncodeError" not in blob3,
          note="R-18：tickets list 期望 rc=0（D1-5）")

    # ④ round --json（dumps(ensure_ascii=False) 直出中文 + 中文阶段日志）
    ev4 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "1", "--dry-run", "--json", "--db", str(db),
                   "--workdir", str(ctx.tmp / "a10-work")], clean_env=True)
    blob4 = ev4["stdout"] + ev4["stderr"]
    c.add(cmd=ev4["cmd"] + "    [clean-env]", rc=ev4["rc"], stdout=ev4["stdout"],
          stderr=ev4["stderr"],
          ok=ev4["rc"] in (EXIT_PASS, EXIT_NEEDS_FIX, EXIT_BLOCKED, EXIT_CONFIRM)
             and "UnicodeEncodeError" not in blob4,
          note="R-18：round --json 的 rc 必须符合 D1-5（0/10/11/3 均可，不得是编码崩溃的 1）")
    if ev4["rc"] == 1 or "UnicodeEncodeError" in blob4:
        c.fail(f"R-18：`round --json` rc={ev4['rc']}，疑似编码崩溃"
               f"（含 UnicodeEncodeError={'UnicodeEncodeError' in blob4}）。")
    c.add(ok=last_json_line(ev4["stdout"]) is not None,
          note="R-18：clean-env 下 `--json` 末行仍是可解析的 RoundResult（中文 JSON 没崩）")


# ------------------------------------------------------------------ 报告


def render(c: Check, verbose: bool) -> str:
    lines = [f"== {c.cid} {c.title} == [{c.status}]  责任: {c.owner}  依据: {c.reference}"]
    for e in c.evidence:
        if e.cmd:
            lines.append(f"   $ {e.cmd}")
            lines.append(f"     -> rc={e.rc} [{'OK' if e.verdict else 'NG'}]"
                         + (f"  {e.note}" if e.note else ""))
        elif e.note:
            lines.append(f"   - {'OK' if e.verdict else 'NG'} {e.note}")
        body, err = (e.stdout or "").strip(), (e.stderr or "").strip()
        if body and (verbose or not e.verdict):
            lines.append("     输出: " + excerpt(body).replace("\n", "\n     "))
        if err and (verbose or not e.verdict):
            lines.append("     stderr: " + excerpt(err).replace("\n", "\n     "))
    for f in c.findings:
        lines.append("   ! " + f.replace("\n", "\n     "))
    return "\n".join(lines)


CHECKS = [
    ("A1", "CLI 可用：--help 退出码 0，含 init/round/tickets/history + 退出码表", "磐石+界面",
     "p0-plan §4/A1、R-1", check_a1),
    ("A2", "离线端到端：dry-run 整轮，票五要素+票号规则、任务包六段、results、六表落库", "磐石+长流",
     "p0-plan §4/A2、D1-1/D1-9/D1-10", check_a2),
    ("A3", "硬规则：未关闭 HIGH 票 → 退出码 3 + 中文；关票后放行；R-6 重跑保留状态", "长流+界面",
     "p0-plan §4/A3、D1-5、R-6", check_a3),
    ("A4", "三态闭环：G1–G6 判据 + 同类第 2 次失败 BLOCKED + AVOID 回灌", "长流",
     "p0-plan §4/A4、D1-3/D1-4/D1-11、R-4", check_a4),
    ("A5.1", "模型绑定铁律：src/ 业务代码无越界模型字符串（llm.py 白名单外）", "全员",
     "p0-plan §4/A5、R-11.1", check_a5_business_scan),
    ("A5.2", "白名单文件自身无字面量模型串（只能从 config/models.yaml 读）", "磐石",
     "p0-rulings R-11.2", check_a5_literal),
    ("A5.3", "适配器：4 角色、MockLLM 确定性、非法 role 拒绝、high_impact 开关", "磐石",
     "p0-plan §3.3、R-3/R-10", check_a5_adapter),
    ("A6", "快照可查：PASS 轮落 snapshots + tag kfpa-r{n} + history 列出", "磐石",
     "p0-plan §4/A6、D1-6", check_a6),
    ("A7", "台账：列全/过滤/close 落库/batch-close 映射与未知票号事务拒绝", "界面",
     "p0-plan §4/A7、D1-8", check_a7),
    ("A7.2", "R-8 impact 下推 Store + R-9 batch-close 幂等与空答案事务", "磐石+界面",
     "p0-rulings R-8/R-9", check_a7_rules),
    ("A8", "LangGraph 图冒烟（build_graph 可编译可跑），纯函数核心仍有保险丝", "长流",
     "p0-plan §4/A8、D1-12", check_a8),
    ("A9", "LiteLLM 适配器异常统一包装为中文 RuntimeError 且提示 --provider mock", "磐石",
     "p0-rulings R-7", check_a9),
    ("A10", "中文输出健壮性：重定向且不设 PYTHONIOENCODING 时 --help/init/tickets list/round --json 不崩", "磐石+界面",
     "p0-rulings R-18", check_a10),
]


def _force_utf8_stdio() -> None:
    """R-19：`scripts/**` 是独立脚本，不经过 `cli.main()`，必须自己做与 R-18.1 等价的 stdio 兜底。

    没有这层，在 Windows 上把输出重定向到文件/管道（stdout 被判定为 cp1252 且非 tty）时，
    本脚本自己的中文 print 就会 UnicodeEncodeError —— 台架先于被测代码崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()   # R-19：必须在任何 print 之前
    ap = argparse.ArgumentParser(description="KFPA P0 独立验收台架（A1–A10）")
    ap.add_argument("--root", default=str(REPO_ROOT), help="仓库根（默认脚本上级目录）")
    ap.add_argument("--only", default="", help="只跑指定断言，逗号分隔，支持前缀（如 A2,A5）")
    ap.add_argument("--json", dest="json_path", default="", help="把完整证据写到 JSON")
    ap.add_argument("--list", action="store_true", help="列出全部断言即退出")
    ap.add_argument("--keep-temp", action="store_true", help="保留临时库")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每条证据的输出")
    args = ap.parse_args(argv)

    if args.list:
        for cid, title, owner, ref, _ in CHECKS:
            print(f"{cid:5s} [{owner}] {title}   ({ref})")
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
    print(f"python={ctx.python}   （R-2：所有命令注入 PYTHONPATH=src）")
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

    n_pass = sum(1 for x in results if x.status == "PASS")
    n_fail = sum(1 for x in results if x.status == "FAIL")
    n_skip = sum(1 for x in results if x.status == "SKIP")
    print(f"汇总：{len(results)} 条断言 -> PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}")
    for x in results:
        if x.status != "PASS":
            print(f"  [{x.status}] {x.cid} {x.title}  (责任: {x.owner})")

    if args.json_path:
        payload = {
            "root": str(root), "python": ctx.python,
            "contract_versions_supported": list(SUPPORTED_CONTRACT_VERSIONS),
            "missing_files": [f for f in REQUIRED_P0_FILES if not (root / f).exists()],
            "summary": {"total": len(results), "pass": n_pass, "fail": n_fail, "skip": n_skip},
            "checks": [{
                "cid": x.cid, "title": x.title, "owner": x.owner, "reference": x.reference,
                "status": x.status, "findings": x.findings,
                "evidence": [{"cmd": e.cmd, "rc": e.rc, "verdict": e.verdict, "note": e.note,
                              "stdout": (e.stdout or "")[:4000],
                              "stderr": (e.stderr or "")[:4000]} for e in x.evidence],
            } for x in results],
        }
        Path(args.json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        print(f"JSON 证据 -> {args.json_path}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

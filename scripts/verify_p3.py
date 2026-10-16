#!/usr/bin/env python3
"""KFPA P3 独立验收台架（A23–A27，另加 A0 = P0/P1/P2 不回归 + pytest 全绿 + 覆盖率不下降）。

依据：`docs/dev/p3-plan.md`（D5 冻结 v1.0）§3 冻结接口 + §4 验收 A23–A27 + §7 合并门禁。
口径沿用：R-19（stdio 兜底）、R-25.3（子进程与本进程都剥 `KFPA_DB_URL`）、R-27（驱动归一）、
R-28（回灌只取本轮+上一轮）、R-29（升版守卫必须覆盖本台架）、R-30（实现方桩交镜鉴搬运）、
R-31（rc 三分：未知值 2 / 依赖凭据 4 / IO 与 DB 5）、R-32（覆盖率只算纯逻辑白名单）。

用法::

    python scripts/verify_p3.py                 # 全量
    python scripts/verify_p3.py --only A24      # 单条
    python scripts/verify_p3.py --json out.json --coverage-out cov.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: D5 §3.1：P3 用契约 1.4
CONTRACT_VERSION_LATEST = "1.4"
SUPPORTED_CONTRACT_VERSIONS = ("1.4",)

EXIT_PASS, EXIT_USAGE, EXIT_CONFIRM, EXIT_DEP, EXIT_IO, EXIT_NEEDS_FIX, EXIT_BLOCKED = \
    0, 2, 3, 4, 5, 10, 11

#: D5 §3：P3 增量文件 -> 唯一写者
P3_FILES = {
    "src/kfpa/fastlane.py": "长流",
    "src/kfpa/knowledge.py": "长流",
    "src/kfpa/rag.py": "长流",
    "migrations/0003_fastlane.sql": "磐石",
    "docs/dev/p3-plan.md": "Chief",
}

#: D5 §3.2：快车道路库表的冻结列（来自 `FastlaneRequest`）
FASTLANE_COLS = {"request_id", "round_no", "summary", "diff_files", "new_interface",
                 "new_data_model", "touched_modules", "status", "batch_id",
                 "decided_by", "decided_at", "note"}
#: D5 §3.3：抽检留痕表（来自 `KnowledgeSample`）
KNOWLEDGE_SAMPLE_COLS = {"cid", "sampled_at", "sampled_by", "rate", "passed"}

#: R-32：A20 门槛白名单（P3 沿用；`store/postgres.py` 只作报告项）
COVER_WHITELIST = (
    "src/kfpa/schemas.py", "src/kfpa/config.py", "src/kfpa/cli.py",
    "src/kfpa/gates.py", "src/kfpa/pipeline.py",
    "src/kfpa/review.py", "src/kfpa/eval.py",
    "src/kfpa/store/__init__.py", "src/kfpa/store/sqlite.py",
)
COVER_WHITELIST_PREFIX: tuple[str, ...] = ()
COVER_EXTERNAL_FILES = ("src/kfpa/store/postgres.py",)
COVER_THRESHOLD = 0.80
#: R-33 配套（#59）：P3 新模块的 A20 增量 —— `knowledge.py` / `fastlane.py` **各 ≥80%**。
#: 生效判据（不是「收口时再说」的口头约定，也不能让 A0 在落地前假红）：
#:   ① 文件在树上 **且** ② 该模块的 P3 冻结接口已落地 → 进门槛，缺一即 FAIL；
#:   只缺 ①/② 时 A23–A25 本来就是红的派工单，这里只作**可见报告项**，A0 保持绿。
#:   注意「躲覆盖」不成立：不落接口 → A24/A25 直接红，红点只是换了检查项。
COVER_P3_GATE: dict[str, tuple[str, str]] = {
    "src/kfpa/fastlane.py": ("长流", "judge"),            # D5 §3.2 冻结纯函数
    "src/kfpa/knowledge.py": ("长流", "record_sample"),   # D5 §3.3 P3 新增出口
}
#: 只作报告项的 P3 模块：`rag.py` 的 Dify 分支在无凭据环境不可达（#59：与 store/postgres.py 同理）
COVER_P3_REPORT_ONLY = ("src/kfpa/rag.py",)
#: A26 复用 A12 夹具（R-33）：KFPA_PG_PORT 优先、默认 55440；55432-55434 禁用（R-22.1）
DEFAULT_PG_PORT = "55440"
FORBIDDEN_PG_PORTS = ("55432", "55433", "55434")
PG_COMPOSE = "docker-compose.yml"
#: A26 双库对拍用**每进程唯一轮号**（A12 的 PROBE_ROUND 用 1000+，这里错开 20000 段），
#: 免得共享 PG 库里别的进程那一轮的行混进 R-28 的「本轮+上一轮」窗口。
A26_ROUND_BASE = 20000
A26_ROUND_SPAN = 5000
#: P2 收口时的白名单覆盖率（main `274c1c1` 实测）——只作对比基线，P3 门槛仍是 80%
P2_WHITELIST_BASELINE = 0.907


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
    coverage_out: str = ""

    def __post_init__(self) -> None:
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(self.root / "src") + os.pathsep + self.env.get("PYTHONPATH", "")
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["PYTHONUTF8"] = "1"
        # R-25.3：子进程与本进程都剥掉 KFPA_DB_URL，免得残留把临时库顶掉
        self.env.pop("KFPA_DB_URL", None)
        os.environ.pop("KFPA_DB_URL", None)
        sys.path.insert(0, str(self.root / "src"))

    def run(self, cmd: list[str], *, cwd: Path | None = None, timeout: int = 300,
            clean_env: bool = False, env_over: dict | None = None) -> dict:
        env = self.env
        if clean_env:
            env = {k: v for k, v in self.env.items()
                   if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        if env_over:
            env = dict(env, **env_over)
        try:
            p = subprocess.run(cmd, cwd=str(cwd or self.root), env=env,
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"cmd": " ".join(cmd), "rc": -9, "stdout": "",
                    "stderr": f"命令超时（>{timeout}s）"}
        return {"cmd": " ".join(cmd), "rc": p.returncode,
                "stdout": p.stdout.decode("utf-8", "replace"),
                "stderr": p.stderr.decode("utf-8", "replace")}

    def cli(self, *args: str, db: Path | None = None, timeout: int = 300,
            env_over: dict | None = None) -> dict:
        cmd = [self.python, "-m", "kfpa", *args]
        if db is not None:
            cmd += ["--db", str(db)]
        return self.run(cmd, timeout=timeout, env_over=env_over)

    def fresh_db(self, name: str) -> Path:
        p = self.tmp / f"{name}.db"
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
        return p

    def design(self) -> Path:
        return self.root / "examples/design-r1.md"

    def init_db(self, db: Path) -> dict:
        return self.cli("init", db=db)


def import_mod(name: str):
    try:
        import importlib
        return importlib.import_module(name), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def excerpt(text: str, n: int = 700) -> str:
    text = (text or "").rstrip()
    if len(text) <= n:
        return text
    return text[: n // 2] + "\n      ...<输出截断>...\n" + text[-n // 2:]


def normalize_rows(rows, *, scalar_when_single: bool = True) -> list:
    """R-27：PG 元组行 / SQLite 标量行归一后再比较。"""
    out = []
    for r in rows or []:
        if r is None:
            continue
        if isinstance(r, (list, tuple)):
            out.append(r[0] if scalar_when_single and len(r) == 1 else list(r))
        else:
            out.append(r)
    return out


def last_json_line(blob: str):
    for line in reversed((blob or "").splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return None


def probe_sqlite(db: Path, table: str | None = None):
    """只读探针：返回 (tables, meta 字典, table 列集合)。"""
    con = sqlite3.connect(str(db))
    try:
        tables = {normalize_rows(con.execute(
            "select name from sqlite_master where type='table'").fetchall())[i]
            for i in range(len(con.execute(
                "select name from sqlite_master where type='table'").fetchall()))}
        meta = {}
        if "meta" in tables:
            for k, v in con.execute("select key, value from meta").fetchall():
                meta[str(k)] = str(v)
        cols = set()
        if table and table in tables:
            cols = {r[1] for r in con.execute(f"pragma table_info({table})").fetchall()}
        return sorted(tables), meta, cols
    finally:
        con.close()


def cover_stats_all(cov: dict) -> dict:
    out: dict[str, dict] = {}
    for path, info in (cov.get("files") or {}).items():
        norm = str(path).replace("\\", "/").lstrip("./")
        summary = (info or {}).get("summary") or {}
        n = int(summary.get("num_statements", 0) or 0)
        c = int(summary.get("covered_lines", 0) or 0)
        out[norm] = {"statements": n, "covered": c, "rate": (c / n) if n else 0.0}
    return out


def cover_rate(per_file: dict, wanted=COVER_WHITELIST, prefixes=COVER_WHITELIST_PREFIX):
    picked = sorted(p for p in per_file
                    if p in wanted or any(p.startswith(x) for x in prefixes))
    covered = sum(per_file[p]["covered"] for p in picked)
    stmts = sum(per_file[p]["statements"] for p in picked)
    return ((covered / stmts) if stmts else 0.0), covered, stmts, picked


def read_env_file(path: Path) -> dict:
    """读 `KEY=VALUE` 形式的 acks.yaml/清单（只支持扁平映射，够 A23 用）。"""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip().strip('"')] = v.strip().strip('"')
    return out


# ------------------------------------------------------------------ A0


def check_a0(ctx: Ctx, c: Check) -> None:
    """D5 §4 A0：P0/P1/P2 三条台架不回归 + `pytest` 全绿 + R-32 白名单覆盖率不下降。"""
    sc, serr = import_mod("kfpa.schemas")
    if sc is None:
        c.add(ok=False, note=f"import kfpa.schemas 失败：{serr}")
        c.fail("A0：契约不可导入。")
    else:
        ver = str(getattr(sc, "CONTRACT_VERSION", ""))
        c.add(ok=ver in SUPPORTED_CONTRACT_VERSIONS,
              note=f"契约版本 = {ver!r}；P3 台架对齐 {SUPPORTED_CONTRACT_VERSIONS}（R-29 升版守卫）")
        if ver not in SUPPORTED_CONTRACT_VERSIONS:
            c.fail(f"A0：契约版本 {ver} 未同步到 P3 台架 —— 升版必须同批更新 "
                   f"scripts/verify_p3.py 的 SUPPORTED_CONTRACT_VERSIONS。")
        # R-29：升版要覆盖仓库里每个台架与 tests/** 的版本清单
        for rel in ("scripts/verify_p0.py", "scripts/verify_p1.py", "scripts/verify_p2.py"):
            text = (ctx.root / rel).read_text(encoding="utf-8")
            ok = CONTRACT_VERSION_LATEST in text
            c.add(ok=ok, note=f"A0：{rel} 的支持集含 {CONTRACT_VERSION_LATEST}（R-29）")
            if not ok:
                c.fail(f"A0：{rel} 的 SUPPORTED_CONTRACT_VERSIONS 未收 {CONTRACT_VERSION_LATEST}。")

    for script, need_total in (("scripts/verify_p0.py", 13), ("scripts/verify_p1.py", 8),
                               ("scripts/verify_p2.py", 6)):
        path = ctx.root / script
        if not path.exists():
            c.fail(f"A0：缺 {script}（P0–P2 门禁不许丢）。")
            continue
        out = ctx.tmp / f"{Path(script).stem}-regression.json"
        ev = ctx.run([ctx.python, str(path), "--json", str(out)], clean_env=True, timeout=1800)
        c.add(cmd=ev["cmd"] + "    [clean-env]", rc=ev["rc"], stdout=ev["stdout"],
              stderr=ev["stderr"], ok=out.exists(),
              note=f"A0：{script} clean env 全量必须产出证据 JSON")
        if not out.exists():
            c.fail(f"A0：{script} 没产出证据文件（rc={ev['rc']}）。")
            continue
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            c.fail(f"A0：{script} 证据 JSON 无法解析：{exc}")
            continue
        s = data.get("summary", {})
        c.add(ok=s.get("fail") == 0 and s.get("total", 0) >= need_total,
              note=f"A0：{script} 汇总 = {s}（要求 fail=0 且 total>={need_total}）")
        if s.get("fail") != 0 or s.get("total", 0) < need_total:
            bad = [x["cid"] for x in data.get("checks", []) if x.get("status") != "PASS"]
            c.fail(f"A0：{script} 回归 —— 非 PASS = {bad}，汇总 = {s}。")

    # `pytest` 必须 rc=0（#52 的教训：A20 只看报告落盘，红套件能藏在绿断言后面）
    cov_json = ctx.tmp / "coverage-p3.json"
    ev = ctx.run([ctx.python, "-m", "pytest", "--cov=kfpa",
                  "--cov-report=json:" + str(cov_json), "-q"], timeout=2400,
                 env_over={"COVERAGE_FILE": str(ctx.tmp / ".coverage-p3")})
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS and cov_json.exists(),
          note="A0：`pytest` 必须 rc=0（红了不许靠覆盖率绿的假象蒙过去）")
    if ev["rc"] != EXIT_PASS:
        tail = (ev["stdout"] or "")[-800:]
        c.fail(f"A0：`pytest` rc={ev['rc']}（必须 0）。尾部输出：\n{tail}")
    if not cov_json.exists():
        c.fail("A0：pytest 没产出覆盖率 JSON。")
        return

    try:
        cov = json.loads(cov_json.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        c.fail(f"A0：覆盖率 JSON 解析失败：{exc}")
        return
    per_file = cover_stats_all(cov)
    rate, covered, stmts, picked = cover_rate(per_file)
    missing = [f for f in COVER_WHITELIST if f not in per_file]
    c.add(ok=not missing, note=f"A0：R-32 白名单文件全部出现在报告（缺 {missing or '无'}）")
    if missing:
        c.fail(f"A0：白名单文件未出现在覆盖率报告 = {missing}（测试从没触达 → 按 0 计）。")
    total_rate = float((cov.get("totals") or {}).get("percent_covered", 0)) / 100.0
    c.add(ok=True, note=f"A0：全仓库行覆盖率 = {total_rate:.1%}；"
                        f"R-32 纯逻辑白名单 = {rate:.1%}（{covered}/{stmts}）"
                        f"；P2 收口基线 {P2_WHITELIST_BASELINE:.1%}")
    c.add(ok=rate >= COVER_THRESHOLD,
          note=f"A0：R-32 白名单 {rate:.1%} >= {COVER_THRESHOLD:.0%}")
    if rate < COVER_THRESHOLD:
        gaps = sorted(((v["statements"] - v["covered"], k) for k, v in per_file.items()
                       if k in picked), reverse=True)
        c.fail(f"A0（硬验收）：R-32 白名单覆盖率 {rate:.1%} < {COVER_THRESHOLD:.0%}；缺口："
               + "、".join(f"{Path(k).name} 缺 {n}" for n, k in gaps[:5] if n))
    if rate < P2_WHITELIST_BASELINE:
        c.warn(f"A0：R-32 白名单覆盖率 {rate:.1%} 低于 P2 收口基线 {P2_WHITELIST_BASELINE:.1%}"
               "（新增语句会拉低比率；低于 80% 才算 FAIL，这里只提示）。")
    # R-33 配套（#59）：P3 新模块的 A20 增量门槛（各 ≥80%）——生效判据见 COVER_P3_GATE 注释
    for f, (owner, iface) in COVER_P3_GATE.items():
        if not (ctx.root / f).exists():
            c.add(ok=True, note=f"A0（报告项）：P3 模块 {f} 尚未落地——红点归 A23–A25 的派工单（{owner}）")
            continue
        mod, merr = import_mod("kfpa." + Path(f).stem)
        if mod is None or not callable(getattr(mod, iface, None)):
            c.add(ok=True, note=f"A0（报告项）：{f} 在树上但冻结出口 `{iface}` 未落地"
                                f"（import={merr or 'ok'}）——先由 A24/A25 判红，覆盖率暂只报告")
            continue
        info = per_file.get(f)
        if info is None:
            c.add(ok=False, note=f"A0：{f} 已落地（`{iface}` 可调用）却没出现在覆盖率报告")
            c.fail(f"A0（硬验收）：P3 模块 {f} 已落地却被测试完全绕过（按 0 计）。")
            continue
        c.add(ok=info["rate"] >= COVER_THRESHOLD,
              note=f"A0：P3 模块 {f} 行覆盖 {info['rate']:.1%}"
                   f"（{info['covered']}/{info['statements']}）>= {COVER_THRESHOLD:.0%}（#59）")
        if info["rate"] < COVER_THRESHOLD:
            c.fail(f"A0（硬验收）：P3 模块 {f} 覆盖率 {info['rate']:.1%} < {COVER_THRESHOLD:.0%}"
                   f"（#59：knowledge/fastlane 各 ≥80%），缺 "
                   f"{info['statements'] - info['covered']} 语句。")
    # 只作报告项（无凭据环境下不可达的分支）
    for f in COVER_P3_REPORT_ONLY:
        info = per_file.get(f)
        if info is None:
            c.add(ok=True, note=f"A0（报告项）：{f} 未出现在报告——模块还没落地或没被测试触达")
        else:
            c.add(ok=True, note=f"A0（报告项）：{f} 行覆盖 {info['rate']:.1%}"
                                f"（{info['covered']}/{info['statements']}）")
            if info["statements"] and info["rate"] < COVER_THRESHOLD:
                c.warn(f"A0：报告项 {f} 覆盖率 {info['rate']:.1%} < {COVER_THRESHOLD:.0%}"
                       "（#59 定为报告项：Dify 分支无凭据不可达；门槛见 COVER_P3_GATE）。")
    ext = [p for p in per_file if p in COVER_EXTERNAL_FILES]
    for p in ext:
        c.add(ok=True, note=f"A0（A20b 口径报告项，非门槛）：{p} 行覆盖 "
                            f"{per_file[p]['rate']:.1%}"
                            f"（{per_file[p]['covered']}/{per_file[p]['statements']}）")
    if ctx.coverage_out:
        target = Path(ctx.coverage_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "totals": {"percent_covered": rate * 100.0},
            "files": {k: {"summary": {"percent_covered": v["rate"] * 100.0,
                                      "num_statements": v["statements"],
                                      "covered_lines": v["covered"]}}
                      for k, v in per_file.items()},
            "critical_paths_ok": True,
            "generated_by": "scripts/verify_p3.py A0",
            "whitelist_files": picked, "whitelist_statements": stmts,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        c.add(ok=target.exists(), note=f"A0：合并覆盖率报告已落盘 → {target}")


# ------------------------------------------------------------------ P3 公共探针


def _cli_ready(ctx: Ctx, c: Check, *subcommands: str) -> bool:
    """子命令是否挂上（磐石的 cli.py 插件清单）。

    **不许用 `--help` 的子串判断**：帮助文本里出现「fastlane」这类词不代表子命令挂上了
    （实测 `kfpa --help` 的 epilog 就提到过快车道）——逐个跑 `kfpa <cmd> --help` 看 rc。
    """
    missing = []
    for sub in subcommands:
        ev = ctx.cli(sub, "--help")
        ok = ev["rc"] == EXIT_PASS
        c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"][:400], stderr=ev["stderr"][:400],
              ok=ok, note=f"子命令 `kfpa {sub}` 可用 = {ok}")
        if not ok:
            missing.append(sub)
    if missing:
        c.fail(f"子命令未挂载：{missing} → 磐石 `cli.py` 的插件清单（`_register_plugin_commands`）"
               "；长流实现 `register()`。")
        return False
    return True


def _sub_ready(ctx: Ctx, c: Check, group: str, *subs: str) -> bool:
    """二级子命令探测：`fastlane` 与 `knowledge` 是 P1 就挂上的（只有 `check` / `list`），
    P3 新增的是 `plan`/`ack`/… 与 `sample`/`verdict`/…，必须逐条验 `kfpa <group> <sub> --help`。"""
    missing = []
    for sub in subs:
        ev = ctx.cli(group, sub, "--help")
        ok = ev["rc"] == EXIT_PASS
        c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"][:300], stderr=ev["stderr"][:300],
              ok=ok, note=f"子命令 `kfpa {group} {sub}` 可用 = {ok}")
        if not ok:
            missing.append(sub)
    if missing:
        c.fail(f"`kfpa {group}` 下缺子命令 {missing} → 长流 `register()` / 磐石插件清单。")
        return False
    return True


def _p3_db(ctx: Ctx, c: Check, name: str, *, need_fastlane: bool) -> Path | None:
    """建临时库并核对 P3 schema（D5 §3.1：`schema_version='4'`）。"""
    db = ctx.fresh_db(name)
    ev = ctx.init_db(db)
    c.add(cmd=ev["cmd"], rc=ev["rc"], ok=ev["rc"] == EXIT_PASS, note=f"建库 {db.name}")
    if ev["rc"] != EXIT_PASS:
        c.fail(f"`kfpa init` rc={ev['rc']}，后续无法验证。")
        return None
    tables, meta, _ = probe_sqlite(db)
    c.add(ok=meta.get("schema_version") == "4",
          note=f"schema_version = {meta.get('schema_version')!r}（D5 §3.1 要求 '4'）")
    if meta.get("schema_version") != "4":
        c.fail(f"schema_version={meta.get('schema_version')!r}，期望 '4'"
               "（0003_fastlane.sql 随批升版，磐石）。")
    if need_fastlane:
        c.add(ok="fastlane_requests" in tables, note=f"库表 = {tables}")
        if "fastlane_requests" not in tables:
            c.fail("缺 `fastlane_requests` 表（磐石 migrations/0003_fastlane.sql）。")
            return db
        _t, _m, cols = probe_sqlite(db, "fastlane_requests")
        lack = sorted(FASTLANE_COLS - cols)
        c.add(ok=not lack, note=f"`fastlane_requests` 列 = {sorted(cols)}（缺 {lack or '无'}）")
        if lack:
            c.fail(f"`fastlane_requests` 缺列 {lack}（D5 §3.1 的 FastlaneRequest 冻结字段）。")
    return db


def _fastlane_db(ctx: Ctx, c: Check, name: str) -> Path | None:
    """A23/A24 用：要求 fastlane 表齐备。"""
    return _p3_db(ctx, c, name, need_fastlane=True)


def _fastlane_rows(db: Path) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("select * from fastlane_requests order by request_id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("diff_files"), str):
                try:
                    d["diff_files"] = json.loads(d["diff_files"])
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(d.get("touched_modules"), str):
                try:
                    d["touched_modules"] = json.loads(d["touched_modules"])
                except Exception:  # noqa: BLE001
                    pass
            out.append(d)
        return out
    finally:
        con.close()


# ------------------------------------------------------------------ A23


def _r34_fastlane_owner(ctx: Ctx, c: Check) -> None:
    """R-34 判据（三）：`kfpa/fastlane.py` 是 `fastlane` 组唯一 owner，`check` 未失效。

    单独拿出来、放在 `_cli_ready` 之前跑：组整个不在时也要看得见（而不是被早退吞掉）。
    """
    help_ev = ctx.cli("fastlane", "--help")
    text = (help_ev["stdout"] or "") + (help_ev["stderr"] or "")
    need = ("check", "plan", "list", "ack", "batch-ack", "show")
    missing = [x for x in need if x not in text]
    c.add(cmd=help_ev["cmd"], rc=help_ev["rc"], stdout=help_ev["stdout"][:600],
          stderr=help_ev["stderr"][:400], ok=help_ev["rc"] == EXIT_PASS and not missing,
          note=f"R-34：`kfpa fastlane --help` 子命令齐（缺 {missing or '无'}）；owner 归 `kfpa/fastlane.py`")
    if help_ev["rc"] != EXIT_PASS:
        c.fail("R-34：`kfpa fastlane` 组不可用 → 磐石 `cli.py` 清单（`kfpa.fastlane`）+ 长流 `register()`。")
        return
    if missing:
        c.fail(f"R-34：`fastlane` 组缺子命令 {missing}（唯一 owner 收口后 `check` 必须一并由它注册）。")

    chk = ctx.cli("fastlane", "check", "--help")
    c.add(cmd=chk["cmd"], rc=chk["rc"], stdout=chk["stdout"][:400], stderr=chk["stderr"][:400],
          ok=chk["rc"] == EXIT_PASS,
          note="R-34：`fastlane check` 换 owner 后仍可用（行为一致性由 A0 内嵌的 verify_p1 断）")
    if chk["rc"] != EXIT_PASS:
        c.fail("R-34：`kfpa fastlane check` 挂了 —— 换 owner 不许把 P1 的 `check` 弄丢（R-24.1）。")

    imp = ctx.cli("impact", "--help")
    c.add(cmd=imp["cmd"], rc=imp["rc"], ok=imp["rc"] != EXIT_PASS,
          note=f"R-34：`kfpa impact` 组不存在（rc={imp['rc']}）—— 注：这条在收口前就成立，"
               "因为 `impact.py` 注册的组名本来就是 `fastlane`，真正的判据见下一条")
    if imp["rc"] == EXIT_PASS:
        c.fail("R-34：`kfpa impact` 组存在 —— 与 R-34 的唯一 owner 收口不符。")

    # 非空断：直接问 `kfpa.impact.register()` 还会不会抢注 `fastlane` 组（真能证伪的那条）
    probe = (
        "import argparse\n"
        "from kfpa import impact\n"
        "p = argparse.ArgumentParser(prog='kfpa')\n"
        "sub = p.add_subparsers(dest='cmd')\n"
        "impact.register(sub)\n"
        "print('REGISTERED:' + ','.join(sorted(sub.choices)))\n"
    )
    ev = ctx.run([ctx.python, "-c", probe], timeout=60)
    registered = ""
    for line in (ev["stdout"] or "").splitlines():
        if line.startswith("REGISTERED:"):
            registered = line.split(":", 1)[1].strip()
    hijack = "fastlane" in [x for x in registered.split(",") if x]
    c.add(cmd="python -c <impact.register 抢注探针>", rc=ev["rc"], stdout=ev["stdout"],
          stderr=ev["stderr"], ok=ev["rc"] == 0 and not hijack,
          note=f"R-34：`kfpa.impact.register()` 抢注子命令 = {registered or '（无）'}"
               f"；抢注 `fastlane` = {hijack}（期望 False，`analyze()` 保留为库函数）")
    if ev["rc"] != 0:
        c.fail(f"R-34：抢注探针跑不起来（rc={ev['rc']}）—— `kfpa.impact` 应仍可导入。")
    elif hijack:
        c.fail("R-34：`kfpa.impact.register()` 仍在 `add_parser('fastlane')` —— "
               "唯一 owner 必须归 `kfpa/fastlane.py`，否则两组撞名会整组消失。")


def _r34_acks_schema(ctx: Ctx, c: Check, db: Path) -> None:
    """R-34 判据（二）：`acks.yaml` 冻结 `acks: [{request_id, note?}]`；坏形状 → rc=2 + 中文。"""
    cases = {
        "缺 `acks` 键": "note: 只有备注，没有 acks\n",
        "元素缺 `request_id`": "acks:\n  - note: 只有备注没有 id\n",
        "`acks` 不是列表": "acks: fl-1-1\n",
    }
    for i, (label, text) in enumerate(cases.items()):
        bad = ctx.tmp / f"acks_bad_{i}.yaml"
        bad.write_text(text, encoding="utf-8")
        ev = ctx.cli("fastlane", "batch-ack", "--file", str(bad), "--by", "verify", db=db)
        blob = (ev["stdout"] or "") + (ev["stderr"] or "")
        c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"][:400], stderr=ev["stderr"][:400],
              ok=ev["rc"] == EXIT_USAGE and cjk(blob),
              note=f"R-34：acks.yaml「{label}」→ rc={ev['rc']}（期望 {EXIT_USAGE}=用法错误）+ 中文")
        if ev["rc"] != EXIT_USAGE:
            c.fail(f"R-34：acks.yaml「{label}」应 rc={EXIT_USAGE}（用法错误），实际 {ev['rc']}"
                   " —— 不许静默跳过或补行。")
        elif not cjk(blob):
            c.fail(f"R-34：acks.yaml「{label}」报错没有中文提示（R-18 口径）。")
        if "Traceback" in blob:
            c.fail(f"R-34：acks.yaml「{label}」抛裸栈（应中文 + rc=2）。")


def check_a23(ctx: Ctx, c: Check) -> None:
    """A23：快车道登记 → 承认落库；幂等；batch_id 同批共享；ESCALATED/REJECTED 不可翻白。"""
    _r34_fastlane_owner(ctx, c)
    if not _cli_ready(ctx, c, "fastlane"):
        return
    if not _sub_ready(ctx, c, "fastlane", "plan", "list", "ack", "batch-ack", "show"):
        return
    db = _fastlane_db(ctx, c, "a23")
    if db is None:
        return

    # R-23 diff 来源三优先级：**不给 `--files` → `no_diff_source` → ESCALATED**，
    # 只有带 `--files`（第二来源）才登记出可承认的 PENDING；先各造一条，两个语义都验。
    plan_esc = ctx.cli("fastlane", "plan", "--round", "2", db=db)
    c.add(cmd=plan_esc["cmd"], rc=plan_esc["rc"], stdout=plan_esc["stdout"], stderr=plan_esc["stderr"],
          note="A23：`fastlane plan --round 2`（不给 `--files`，R-23 第三来源）")
    plan = ctx.cli("fastlane", "plan", "--round", "1", "--files", "src/kfpa/gates.py", db=db)
    c.add(cmd=plan["cmd"], rc=plan["rc"], stdout=plan["stdout"], stderr=plan["stderr"],
          ok=plan["rc"] == EXIT_PASS, note="A23：`fastlane plan --round 1 --files …` 登记（第二来源）")
    if plan["rc"] != EXIT_PASS:
        c.fail(f"A23：`fastlane plan` rc={plan['rc']} → 长流 `fastlane.py` 的 register()/磐石挂载。")
        return

    listing = ctx.cli("fastlane", "list", "--json", db=db)
    rows_json = last_json_line(listing["stdout"])
    c.add(cmd=listing["cmd"], rc=listing["rc"], stdout=listing["stdout"],
          stderr=listing["stderr"], ok=listing["rc"] == EXIT_PASS and rows_json is not None,
          note="A23：`fastlane list --json` 输出稳定列")
    rows = _fastlane_rows(db)
    c.add(ok=bool(rows), note=f"A23：`fastlane_requests` 行数 = {len(rows)}")
    if not rows:
        c.fail("A23：plan 之后表里没有行 → 登记没落库。")
        return
    by_status = {str(r.get("status")): r for r in rows}
    c.add(ok="escalated" in by_status,
          note=f"A23：不给 `--files` 的那条状态 = {by_status.get('escalated', {}).get('status')!r}"
               "（R-23：`no_diff_source` → ESCALATED，不许默认放行）")
    if "escalated" not in by_status:
        c.fail("A23：R-23 第三来源（`no_diff_source`）没有让请求升级 —— 不给 diff 依据就不许当快车道。")
    if "pending" not in by_status:
        c.fail(f"A23：带 `--files` 的登记没产出 pending 行（实际状态集 {sorted(by_status)}）"
               " —— 没有可承认样本，后续幂等/批次都无从验证。")
        return
    first = by_status["pending"]

    # R-34：acks.yaml 冻结形状先断（坏输入不许被静默吞掉）——**用独立库**，
    # 免得「坏形状被静默接受」时顺手改掉主库状态、把后面的幂等/留痕断言带红。
    schema_db = _fastlane_db(ctx, c, "a23-acks")
    if schema_db is not None:
        ctx.cli("fastlane", "plan", "--round", "1", "--files", "src/kfpa/gates.py", db=schema_db)
        _r34_acks_schema(ctx, c, schema_db)
        # 坏形状若被静默接受，主库状态会漂；这里独立记一条「有没有被改」
        rows_before = len(_fastlane_rows(schema_db))
        c.add(ok=rows_before >= 1,
              note=f"A23：acks 坏形状探针用独立库（{schema_db.name}），行数 = {rows_before}，"
                   "不污染主断言")

    ack = ctx.cli("fastlane", "ack", first["request_id"], "--by", "reviewer", db=db)
    c.add(cmd=ack["cmd"], rc=ack["rc"], stdout=ack["stdout"], stderr=ack["stderr"],
          ok=ack["rc"] == EXIT_PASS, note=f"A23：ack {first['request_id']}")
    rows_after = {r["request_id"]: r for r in _fastlane_rows(db)}
    got = rows_after[first["request_id"]]
    c.add(ok=str(got.get("status")) == "approved" and got.get("decided_by") == "reviewer"
          and float(got.get("decided_at") or 0) > 0,
          note=f"A23：ack 后 status={got.get('status')!r} decided_by={got.get('decided_by')!r} "
               f"decided_at={got.get('decided_at')!r}")
    if str(got.get("status")) != "approved":
        c.fail(f"A23：ack 之后状态仍是 {got.get('status')!r}（期望 approved）。")
    if got.get("decided_by") != "reviewer" or float(got.get("decided_at") or 0) <= 0:
        c.fail("A23：ack 没留下 decided_by/decided_at 留痕。")

    # 幂等：二次承认不写新行、不覆盖首次留痕
    n_before = len(_fastlane_rows(db))
    ack2 = ctx.cli("fastlane", "ack", first["request_id"], "--by", "someone-else", db=db)
    rows_after2 = {r["request_id"]: r for r in _fastlane_rows(db)}
    same = rows_after2[first["request_id"]]
    c.add(cmd=ack2["cmd"], rc=ack2["rc"], ok=len(_fastlane_rows(db)) == n_before
          and same.get("decided_by") == "reviewer"
          and float(same.get("decided_at")) == float(got.get("decided_at")),
          note=f"A23：重复 ack 幂等（行数 {n_before}→{len(_fastlane_rows(db))}，"
               f"decided_by 仍 {same.get('decided_by')!r}）")
    if len(_fastlane_rows(db)) != n_before or same.get("decided_by") != "reviewer":
        c.fail("A23：重复承认不幂等（写了新行或覆盖了首次 decided_by/decided_at）。")

    # 攒批承认：同批共享 batch_id（R-34 冻结形状 `acks: [{request_id, note?}]`）
    ctx.cli("fastlane", "plan", "--round", "3", "--files", "src/kfpa/gates.py", db=db)
    pending = [r for r in _fastlane_rows(db) if str(r.get("status")) == "pending"]
    acks = ctx.tmp / "acks.yaml"
    acks.write_text("acks:\n"
                    + "".join(f"  - request_id: {r['request_id']}\n    note: 同批承认\n" for r in pending),
                    encoding="utf-8")
    if pending:
        batch = ctx.cli("fastlane", "batch-ack", "--file", str(acks), "--by", "boss", db=db)
        c.add(cmd=batch["cmd"], rc=batch["rc"], stdout=batch["stdout"], stderr=batch["stderr"],
              ok=batch["rc"] == EXIT_PASS, note=f"A23：batch-ack {len(pending)} 条（冻结 YAML 形状）")
        approved = [r for r in _fastlane_rows(db)
                    if r["request_id"] in {p["request_id"] for p in pending}
                    and str(r.get("status")) == "approved"]
        batch_ids = {r.get("batch_id") for r in approved}
        c.add(ok=bool(approved) and len(batch_ids) == 1 and all(batch_ids),
              note=f"A23：同批 batch_id 集合 = {batch_ids}（{len(approved)} 条已承认）")
        if not (approved and len(batch_ids) == 1 and all(batch_ids)):
            c.fail("A23：batch-ack 没让同批共享同一个非空 batch_id。")
    else:
        c.warn("A23：第二轮 plan 之后没有 pending 行，攒批承认这一步无样本可验。")

    # ESCALATED / REJECTED 不许被 ack 翻成 APPROVED（直接造行，黑盒验守卫）
    con = sqlite3.connect(str(db))
    try:
        for rid, status in (("fl-test-escalated", "escalated"), ("fl-test-rejected", "rejected")):
            con.execute(
                "insert or replace into fastlane_requests"
                "(request_id, round_no, summary, diff_files, new_interface, new_data_model,"
                " touched_modules, status, batch_id, decided_by, decided_at, note)"
                " values (?, 9, '监管样本', '[]', 0, 0, '[]', ?, '', 'tester', 1.0, '')",
                (rid, status))
        con.commit()
    finally:
        con.close()
    for rid, status in (("fl-test-escalated", "escalated"), ("fl-test-rejected", "rejected")):
        ev = ctx.cli("fastlane", "ack", rid, "--by", "someone", db=db)
        row = {r["request_id"]: r for r in _fastlane_rows(db)}[rid]
        c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
              ok=str(row.get("status")) == status,
              note=f"A23：{status} 的请求 ack 后仍是 {row.get('status')!r}")
        if str(row.get("status")) != status:
            c.fail(f"A23：{status} 被 ack 翻成了 {row.get('status')!r}"
                   "（D5 §3.2：要翻必须新开请求）。")


# ------------------------------------------------------------------ A24


def check_a24(ctx: Ctx, c: Check) -> None:
    """A24：三判据全真才 PENDING→APPROVED；任一为假 → ESCALATED 且 reasons 指名。"""
    mod, err = import_mod("kfpa.fastlane")
    if mod is None:
        c.add(ok=False, note=f"import kfpa.fastlane 失败：{err}")
        c.fail("A24：`src/kfpa/fastlane.py` 不可导入（长流）。")
        return
    judge = getattr(mod, "judge", None)
    c.add(ok=callable(judge), note=f"fastlane.judge 可调用 = {callable(judge)}（D5 §3.2 冻结）")
    if not callable(judge):
        c.fail("A24：缺 `judge(request) -> bool`（D5 §3.2 冻结签名）。")

    # R-33：出口名冻结（judge/plan/ack/batch_ack），探不到 = FAIL（名字漂 = 契约漂）
    for name in ("plan", "ack", "batch_ack"):
        ok = callable(getattr(mod, name, None))
        c.add(ok=ok, note=f"fastlane.{name} 可调用 = {ok}（R-33 冻结出口）")
        if not ok:
            c.fail(f"A24：缺冻结出口 `{name}(...)`（D5 §3.2 / R-33）；"
                   f"当前模块顶层有 {sorted(n for n in vars(mod) if not n.startswith('_'))[:12]}…")

    sc, serr = import_mod("kfpa.schemas")
    if sc is None or not hasattr(sc, "FastlaneRequest"):
        c.add(ok=False, note=f"契约缺 FastlaneRequest：{serr}")
        c.fail("A24：契约 1.4 的 `FastlaneRequest` 缺失。")
        return

    def make(**kw):
        base = dict(request_id="fl-1-1", round_no=1, summary="微改", diff_files=["a.py"],
                    new_interface=False, new_data_model=False, touched_modules=["a"])
        base.update(kw)
        return sc.FastlaneRequest(**base)

    clean = make()
    c.add(ok=bool(judge(clean)) is True, note="A24：三判据全真 → 快车道（judge=True）")
    if not judge(clean):
        c.fail("A24：三判据全真却判 False。")

    cases = {
        "new_interface": make(new_interface=True),
        "new_data_model": make(new_data_model=True),
        "touched_modules": make(touched_modules=["a", "b"]),
        "no_diff_source": make(diff_files=[]),
    }
    for label, req in cases.items():
        got = judge(req)
        c.add(ok=got is False, note=f"A24：{label} 为假 → judge={got}（期望 False）")
        if got:
            c.fail(f"A24：{label} 这条判据为假却仍判 True。")
        reasons = []
        escalations = getattr(req, "escalations", None)
        if callable(escalations):
            try:
                reasons = [str(x) for x in escalations()]
            except Exception as exc:  # noqa: BLE001
                reasons = [f"<escalations() 抛 {type(exc).__name__}: {exc}>"]
        c.add(ok=bool(reasons), note=f"A24：{label} 的 reasons = {reasons}")
        if not reasons:
            c.fail(f"A24：{label} 判假但 `FastlaneRequest.escalations()` 给了空 reasons"
                   "（D5：不许只给个布尔）—— 这条断的是**契约方法**（`schemas.py`，Chief 归属）；"
                   "实现方 `fastlane.escalate_reasons()` 已能指名，红点在契约侧。")
        elif label == "no_diff_source":
            if not any("no_diff_source" in r for r in reasons):
                c.fail(f"A24：diff_files 为空时契约 `escalations()` 没指名 `no_diff_source`"
                       f"（R-23 第三来源，`schemas.py` 归属 Chief）：{reasons}")
        elif not any(label in r for r in reasons):
            c.fail(f"A24：reasons 没指名是哪条判据（{label}）：{reasons}")

    # 状态机落库侧：plan 出来的行状态必须是契约里的四值之一
    if _cli_ready(ctx, c, "fastlane"):
        db = _fastlane_db(ctx, c, "a24")
        if db is not None:
            ctx.cli("fastlane", "plan", "--round", "1", db=db)
            rows = _fastlane_rows(db)
            statuses = {str(r.get("status")) for r in rows}
            legal = {"pending", "approved", "rejected", "escalated"}
            c.add(ok=bool(rows) and statuses <= legal,
                  note=f"A24：plan 出来的状态集合 = {statuses}（合法集 {sorted(legal)}）")
            if rows and not statuses <= legal:
                c.fail(f"A24：出现非法 status = {statuses - legal}")


# ------------------------------------------------------------------ A25


def check_a25(ctx: Ctx, c: Check) -> None:
    """A25：置信度门槛 → 直接 DROPPED；QUARANTINE 只被后续轮次复用才转正；
    抽检不过 → DROPPED + 写回避模式；抽检留痕可查。"""
    if not _cli_ready(ctx, c, "knowledge"):
        return
    if not _sub_ready(ctx, c, "knowledge", "list", "sample", "verdict", "report"):
        return
    # D5 自相冲突：A25 验收行列了 `promote`，§3.3 冻结 CLI 只列 sample|verdict|report。
    # 断言跟冻结接口走 → 缺 `promote` 只作可见提示，不判红（请 Chief 下一版 D5 收敛）。
    _pm = ctx.cli("knowledge", "promote", "--help")
    if _pm["rc"] != EXIT_PASS:
        c.warn("A25：`knowledge promote` 未挂 —— D5 验收行列了它、§3.3 冻结 CLI 没有它；"
               "按冻结接口不判红，但口径要收敛。")
    mod, err = import_mod("kfpa.knowledge")
    if mod is None:
        c.add(ok=False, note=f"import kfpa.knowledge 失败：{err}")
        c.fail("A25：`src/kfpa/knowledge.py` 不可导入（长流）。")
        return

    threshold = getattr(mod, "CONFIDENCE_THRESHOLD", None)
    c.add(ok=threshold is not None and abs(float(threshold) - 0.80) < 1e-9,
          note=f"knowledge.CONFIDENCE_THRESHOLD = {threshold!r}（D5 §3.3.1 / A15 原文冻结 0.80；"
               "R-33 已把 D5 v1.0 的 0.6 判为笔误——断言跟口径走，不跟实现走）")
    if threshold is None:
        c.fail("A25：缺 `CONFIDENCE_THRESHOLD` 常量（D5 §3.3.1 冻结 = 0.80）。")
    elif abs(float(threshold) - 0.80) >= 1e-9:
        c.fail(f"A25：门槛 = {threshold!r}，D5 §3.3.1 冻结 0.80（R-33 裁定门槛不回退）。")

    # R-33：闸门入口名冻结（`admit` / `record_sample`），探不到 = FAIL
    gate = getattr(mod, "admit", None)
    sample_fn = getattr(mod, "record_sample", None)
    c.add(ok=callable(gate), note=f"knowledge.admit 可调用 = {callable(gate)}（R-33 冻结入口名）")
    if not callable(gate):
        alias = [n for n in ("gate", "ingest", "apply_gate", "submit_candidate", "judge_candidate")
                 if callable(getattr(mod, n, None))]
        c.fail(f"A25：缺冻结入口 `admit(store, candidate)`（R-33 / D5 §3.3）；"
               f"当前只找到别名 {alias or '无'} —— 名字漂了就是契约漂了。")
    c.add(ok=callable(sample_fn),
          note=f"knowledge.record_sample 可调用 = {callable(sample_fn)}（R-33 冻结出口）")
    if not callable(sample_fn):
        c.fail("A25：缺 `record_sample(store, cid, passed, *, sampled_by, rate)`（D5 §3.3 / R-33 冻结）。")

    can_promote = getattr(mod, "can_promote", None) or getattr(mod, "promotable", None)
    promote_name = ("can_promote" if callable(getattr(mod, "can_promote", None))
                    else "promotable" if callable(getattr(mod, "promotable", None)) else "")
    rate_fn = getattr(mod, "sampling_rate", None)
    c.add(ok=callable(can_promote),
          note=f"knowledge.{promote_name or 'can_promote/promotable'} 可调用 = {callable(can_promote)}"
               "（D5 §3.3 转正判据）")
    if not callable(can_promote):
        c.fail("A25：缺 `can_promote(candidate) -> bool`（D5 §3.3）。")
    c.add(ok=callable(rate_fn), note=f"knowledge.sampling_rate 可调用 = {callable(rate_fn)}")
    if not callable(rate_fn):
        c.fail("A25：缺 `sampling_rate(successful_reuse_count)`（D5 §3.3 沿用 D3-3.5 分档）。")

    sc, _serr = import_mod("kfpa.schemas")
    if sc is None or not hasattr(sc, "KnowledgeCandidate"):
        c.fail("A25：契约缺 `KnowledgeCandidate`。")
        return
    CandidateStatus = getattr(sc, "CandidateStatus", None)

    def cand(cid="K1", round_no=1, conf=0.9, reused_by=None, status=None):
        kw = dict(cid=cid, round_no=round_no, kind="success", content="c", confidence=conf,
                  reused_by=list(reused_by or []))
        if status is not None:
            kw["status"] = status
        return sc.KnowledgeCandidate(**kw)

    # 后续轮次判定（同轮不算）
    same_round = cand(reused_by=[1])          # 只被同轮复用
    later_round = cand(reused_by=[1, 2])      # 被后续轮次复用
    if callable(can_promote):
        got_same = bool(can_promote(same_round))
        got_later = bool(can_promote(later_round))
        c.add(ok=got_same is False, note=f"A25：只被同轮复用 can_promote={got_same}（期望 False）")
        c.add(ok=got_later is True, note=f"A25：被后续轮次复用 can_promote={got_later}（期望 True）")
        if got_same:
            c.fail("A25：同轮复用就允许转正（防自证失效）。")
        if not got_later:
            c.fail("A25：被后续轮次复用却不允许转正。")

    if callable(rate_fn):
        try:
            table = {n: float(rate_fn(n)) for n in (0, 1, 2, 3, 5, 10)}
            c.add(ok=True, note=f"A25：sampling_rate 分档 = {table}")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"sampling_rate 抛 {type(exc).__name__}: {exc}")
            c.fail("A25：`sampling_rate()` 不能对 0..10 次复用给出数值。")

    # A25 只管知识侧：不要求 fastlane 表齐备（那是 A23 的事），但 schema 4 与抽检表是 P3 的
    db = _p3_db(ctx, c, "a25", need_fastlane=False)
    if db is None:
        return
    _t2, _m2, sample_cols = probe_sqlite(db, "knowledge_samples")
    c.add(ok="knowledge_samples" in _t2, note=f"A25：库表 = {_t2}")
    if "knowledge_samples" not in _t2:
        c.fail("A25：缺 `knowledge_samples` 表（磐石 migrations/0003_fastlane.sql，D5 §3.3 留痕）。")

    # 低置信度直接 DROPPED：用 store 写入 + 冻结闸门入口驱动
    if not callable(gate):
        c.add(ok=False, note="A25：闸门入口 `admit` 不可用 —— 低置信度丢弃与抽检两条都无从独立验证")
    else:
        store = None
        store_mod, _e = import_mod("kfpa.store")
        if store_mod is not None:
            try:
                store = store_mod.make_store(str(db))
            except Exception as exc:  # noqa: BLE001
                c.add(ok=False, note=f"make_store 失败：{exc}")
        if store is not None:
            for cid, conf in (("K-low", 0.2), ("K-high", 0.95)):
                store.add_candidate(cand(cid=cid, conf=conf))
            for cid, conf in (("K-low", 0.2), ("K-high", 0.95)):
                try:
                    gate(store, cand(cid=cid, conf=conf))
                except Exception as exc:  # noqa: BLE001
                    c.add(ok=False, note=f"admit({cid}) 抛 {type(exc).__name__}: {exc}")
            low = store.get_candidate("K-low")
            high = store.get_candidate("K-high")
            low_status = str(getattr(low.status, "value", low.status))
            high_status = str(getattr(high.status, "value", high.status))
            c.add(ok=low_status == "dropped",
                  note=f"A25：低置信度(0.2) 状态 = {low_status}（期望 dropped，不进隔离区）")
            if low_status != "dropped":
                c.fail(f"A25：低于门槛的候选状态是 {low_status}，期望 dropped。")
            c.add(ok=high_status in ("quarantine", "promoted"),
                  note=f"A25：过门槛(0.95) 状态 = {high_status}（期望 quarantine/promoted）")
            if high_status not in ("quarantine", "promoted"):
                c.fail(f"A25：过门槛的候选状态是 {high_status}，期望先落 quarantine。")

            # R-33：抽检 passed=False → 该候选 DROPPED + 内容进回避模式 + knowledge_samples 留痕
            if callable(sample_fn):
                try:
                    sample_fn(store, "K-high", False, sampled_by="verify_p3", rate=0.2)
                    c.add(ok=True, note="A25：`record_sample(K-high, passed=False)` 可调用")
                except Exception as exc:  # noqa: BLE001
                    c.add(ok=False, note=f"record_sample 抛 {type(exc).__name__}: {exc}")
                    c.fail("A25：抽检 `passed=False` 路径不可用（D5 §3.3 冻结出口）。")
                after = store.get_candidate("K-high")
                after_status = str(getattr(after.status, "value", after.status))
                c.add(ok=after_status == "dropped",
                      note=f"A25：抽检不过之后 K-high 状态 = {after_status}（期望 dropped）")
                if after_status != "dropped":
                    c.fail(f"A25：抽检 passed=False 没把候选打成 dropped（={after_status}）。")
                con = sqlite3.connect(str(db))
                try:
                    rows = con.execute(
                        "select cid, passed, sampled_by, rate from knowledge_samples").fetchall()
                except sqlite3.Error as exc:
                    rows = []
                    c.add(ok=False, note=f"读 knowledge_samples 失败：{exc}")
                finally:
                    con.close()
                hit = [r for r in rows if str(r[0]) == "K-high"]
                c.add(ok=bool(hit), note=f"A25：knowledge_samples 留痕 = {rows}")
                if not hit:
                    c.fail("A25：抽检没落 knowledge_samples 留痕行（D5 §3.3 留痕可查）。")
                else:
                    c.add(ok=not int(hit[0][1]), note=f"A25：留痕 passed 列 = {hit[0][1]}（期望假=不过）")
                    c.add(ok=str(hit[0][2]) == "verify_p3",
                          note=f"A25：留痕 sampled_by = {hit[0][2]!r}（期望 verify_p3，可追责）")
                # 回避模式：内容要进回避清单。实测实现把它落成 **FailureRecord.avoid_pattern**
                # （`store.list_avoid_patterns()` 的存储面），不是 kind=avoid 的候选 —— 断言跟存储面走。
                avoid_fn = getattr(store, "list_avoid_patterns", None)
                if not callable(avoid_fn):
                    c.warn("A25：store 没暴露 `list_avoid_patterns()`，回避清单未独立核"
                           "（内容级验证见 A26 回灌）。")
                else:
                    try:
                        patterns = [str(x) for x in avoid_fn()]
                        round_patterns = [str(x) for x in avoid_fn(int(after.round_no))]
                    except Exception as exc:  # noqa: BLE001
                        patterns, round_patterns = [], []
                        c.add(ok=False, note=f"list_avoid_patterns 抛 {type(exc).__name__}: {exc}")
                    c.add(ok="c" in patterns,
                          note=f"A25：抽检不过写入回避清单 = {patterns}（应含候选内容 'c'）")
                    if "c" not in patterns:
                        c.fail("A25：抽检 `passed=False` 没把内容写进回避模式清单（D5 §3.3 负知识闭环）。")
                    c.add(ok="c" in round_patterns,
                          note=f"A25：按候选轮次 {after.round_no} 过滤的回避清单 = {round_patterns}")
            store.close()

    # CLI 面：list / report 可跑，且 report 落盘
    lst = ctx.cli("knowledge", "list", "--json", db=db)
    c.add(cmd=lst["cmd"], rc=lst["rc"], stdout=lst["stdout"], stderr=lst["stderr"],
          ok=lst["rc"] == EXIT_PASS, note="A25：`knowledge list --json`")
    out = ctx.tmp / "knowledge-report.json"
    rep = ctx.cli("knowledge", "report", "--out", str(out), db=db)
    c.add(cmd=rep["cmd"], rc=rep["rc"], stdout=rep["stdout"], stderr=rep["stderr"],
          ok=rep["rc"] == EXIT_PASS and out.exists(),
          note=f"A25：`knowledge report --out` 落盘 → {out.name}")
    if rep["rc"] != EXIT_PASS or not out.exists():
        c.fail(f"A25：`knowledge report --out` rc={rep['rc']}，报告没落盘。")
    else:
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
            c.add(ok=True, note=f"A25：报告顶层键 = {sorted(payload)}")
        except Exception as exc:  # noqa: BLE001
            c.fail(f"A25：报告不是合法 JSON：{exc}")

    # 抽检留痕表
    _t, _m, cols = probe_sqlite(db, "knowledge_samples")
    lack = sorted(KNOWLEDGE_SAMPLE_COLS - cols)
    c.add(ok=not lack, note=f"A25：`knowledge_samples` 列 = {sorted(cols)}（缺 {lack or '无'}）")
    if lack:
        c.fail(f"A25：`knowledge_samples` 缺列 {lack}（D5 §3.1 的 KnowledgeSample 冻结字段）。")


# ------------------------------------------------------------------ A26


def check_a26(ctx: Ctx, c: Check) -> None:
    """A26：失败进下一轮 feedback；回灌只取本轮+上一轮（含上界）；两库 package_hash 一致。

    双库对拍沿用 A12 的成熟形态（R-33）：**每进程唯一轮号**避免共享库撞车，
    两侧跑同一序列（注入失败轮 → 净轮）再比 `snapshot.package_hash` 与 feedback。
    """
    if not _cli_ready(ctx, c, "round"):
        return
    base = A26_ROUND_BASE + (os.getpid() % A26_ROUND_SPAN)
    n_old, n_bad, n_clean = base, base + 1, base + 2
    db = ctx.fresh_db("a26")
    ev = ctx.init_db(db)
    c.add(cmd=ev["cmd"], rc=ev["rc"], ok=ev["rc"] == EXIT_PASS, note="A26：建临时库")

    work = ctx.tmp / "a26-work"

    def run_round(num: int, conn, *, inject: str = "", tag: str = "") -> dict:
        argv = [ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                "--round", str(num), "--dry-run", "--json", "--db", conn,
                "--workdir", str(work)]
        if inject:
            argv += ["--inject", inject]
        ev = ctx.run(argv, timeout=600)
        note = f"A26：{tag or ('第 ' + str(num) + ' 轮')}"
        c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"], note=note)
        return ev

    def feedback_of(ev: dict) -> list[str] | None:
        p = last_json_line(ev["stdout"])
        if p is None:
            return None
        return [str(x) for x in ((p.get("task_package") or {}).get("feedback") or [])]

    # ① 更早轮注入失败（它的 AVOID 只允许出现在紧邻的下一轮，不得越窗）
    run_round(n_old, str(db), inject="test_fail", tag=f"最老轮 {n_old} 注入 test_fail（下界样本）")
    # ② 紧邻轮：feedback 必须带出上一轮回避模式
    ev_bad_next = run_round(n_bad, str(db), tag=f"第 {n_bad} 轮（承接 {n_old} 的失败）")
    fb_bad_next = feedback_of(ev_bad_next)
    if fb_bad_next is None:
        c.fail("A26：承接轮没产出可解析 JSON，无法验证回灌。")
        return
    c.add(ok=any(x.startswith("AVOID[") for x in fb_bad_next),
          note=f"A26：第 {n_bad} 轮 feedback = {fb_bad_next}")
    if not any(x.startswith("AVOID[") for x in fb_bad_next):
        c.fail("A26：失败回避模式没进下一轮任务包 feedback（R-28 回灌断裂）。")

    # ③ 再下一轮：窗口是 ② 与 ③，②无失败 → feedback 不许再有 AVOID（R-28 上界：不许全库历史）
    ev_clean = run_round(n_clean, str(db), tag=f"第 {n_clean} 轮（窗口内无失败）")
    fb_clean = feedback_of(ev_clean)
    if fb_clean is None:
        c.fail(f"A26：第 {n_clean} 轮没产出可解析 JSON。")
        return
    c.add(ok=not any(x.startswith("AVOID[") for x in fb_clean),
          note=f"A26：第 {n_clean} 轮 feedback = {fb_clean}（窗口={n_bad},{n_clean}，不应含 AVOID）")
    if any(x.startswith("AVOID[") for x in fb_clean):
        c.fail(f"A26：第 {n_clean} 轮仍带出更早轮（{n_old}）的回避模式 —— 回灌超窗（R-28 只取本轮+上一轮）。")

    # 双库一致（R-33）：复用 A12 夹具 —— KFPA_PG_PORT 优先（默认 55440），
    # KFPA_VERIFY_PG_DSN 显式覆盖；连不上 = 可见 SKIP；**不自建容器**（R-24/R-33）。
    dsn, origin, why = _a26_pg_dsn(ctx)
    c.add(ok=not why, note=f"A26：PG 夹具 {origin}；DSN = {mask_dsn(dsn) if dsn else '（无）'}"
                           + (f"；{why}" if why else ""))
    if why:
        c.fail(f"A26：{why}")
        return
    if not dsn:
        c.skip("A26：KFPA_PG_PORT/KFPA_VERIFY_PG_DSN 都没能给出可用 DSN，PG 侧 "
               "`package_hash` 对拍未跑（报告里必须可见）。")
        return
    if not _pg_reachable(ctx, dsn):
        c.skip(f"A26：{origin} 上连不上 PG（clean env 无 Docker 的正常态）→ PG 侧 "
               "`package_hash` 对拍可见 SKIP；有 PG 的环境设 `KFPA_PG_PORT=<port>` 重跑。")
        return
    c.add(ok=True, note=f"A26：PG 可达（{origin}），跑同一轮序列（轮号 {n_old}/{n_bad}，"
                        f"本进程唯一，避免共享库撞车）")

    ev0 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", dsn], timeout=300)
    c.add(cmd=ev0["cmd"], rc=ev0["rc"], ok=ev0["rc"] == EXIT_PASS, note="A26：PG 侧建库（复用 A12 夹具）")
    if ev0["rc"] != EXIT_PASS:
        c.fail(f"A26：PG 侧 `kfpa init` rc={ev0['rc']}（归磐石 store/postgres.py 建表）。")
        return
    run_round(n_old, dsn, inject="test_fail", tag=f"PG 第 {n_old} 轮注入 test_fail")
    pg_ev = run_round(n_bad, dsn, tag=f"PG 第 {n_bad} 轮")
    pg_payload = last_json_line(pg_ev["stdout"])
    if pg_payload is None:
        c.fail(f"A26：PG 侧没产出 JSON（rc={pg_ev['rc']}）。")
        return
    sqlite_payload = last_json_line(ev_bad_next["stdout"])

    def digest(p):
        """`package_hash` 落在 `snapshot`（实测形状）；兼容 `task_package` 上的旧位置。"""
        snap = (p or {}).get("snapshot") or {}
        return snap.get("package_hash") or ((p or {}).get("task_package") or {}).get("package_hash")

    s_hash, p_hash = digest(sqlite_payload), digest(pg_payload)
    c.add(ok=bool(s_hash) and s_hash == p_hash,
          note=f"A26：第 {n_bad} 轮 package_hash SQLite={s_hash!r} PG={p_hash!r}")
    if not s_hash:
        c.fail("A26：SQLite 侧没读出 package_hash（对拍无从成立，别让 None==None 混过去）。")
    elif s_hash != p_hash:
        c.fail("A26：两库同轮 package_hash 不一致（回灌范围或轮次过滤两端不同口径）。")
    pg_fb = [str(x) for x in ((pg_payload.get("task_package") or {}).get("feedback") or [])]
    c.add(ok=pg_fb == fb_bad_next, note=f"A26：feedback 两库一致 = {pg_fb == fb_bad_next}"
                                        f"（PG={pg_fb}）")
    if pg_fb != fb_bad_next:
        c.fail("A26：两库同一轮的 feedback 不一致（回灌区在两库口径不同）。")


def mask_dsn(dsn: str) -> str:
    """打码 DSN 里的密码，只留口令位形状（报告里不许出现明文凭据）。"""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn or "")


def _a26_pg_dsn(ctx: Ctx) -> tuple[str, str, str]:
    """R-33：A26 复用 A12 夹具。返回 (dsn, 来源说明, 硬错误)。

    - `KFPA_VERIFY_PG_DSN` 显式覆盖优先；
    - 否则按 `KFPA_PG_PORT`（默认 55440）/ R-22.1 组装，凭据读 compose；
    - 禁用端口 55432-55434（R-22.1，别的项目占着）；
    - **不自建容器**：连不上就是可见 SKIP，绝不 `docker compose up`（R-24/R-33）。
    """
    explicit = (os.environ.get("KFPA_VERIFY_PG_DSN") or "").strip()
    if explicit:
        return explicit, "显式 KFPA_VERIFY_PG_DSN", ""
    port = str(os.environ.get("KFPA_PG_PORT") or DEFAULT_PG_PORT).strip()
    if port in FORBIDDEN_PG_PORTS:
        return "", f"KFPA_PG_PORT={port}", f"端口 {port} 属 R-22.1 禁用段（55432-55434）"
    user, pwd, db = "kfpa", "kfpa", "kfpa"
    compose = ctx.root / PG_COMPOSE
    if compose.exists():
        try:
            import yaml
            spec = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
            for svc in (spec.get("services") or {}).values():
                env = svc.get("environment") or {}
                if isinstance(env, list):
                    env = dict(x.split("=", 1) for x in env if "=" in x)
                user = env.get("POSTGRES_USER", user)
                pwd = env.get("POSTGRES_PASSWORD", pwd)
                db = env.get("POSTGRES_DB", db)
                break
        except Exception:  # noqa: BLE001
            pass
    dsn = f"postgresql://{user}:{pwd}@127.0.0.1:{port}/{db}"
    return dsn, f"KFPA_PG_PORT={port}（默认 {DEFAULT_PG_PORT}，A12 夹具）", ""


def _pg_reachable(ctx: Ctx, dsn: str) -> bool:
    """子进程探一次连通性（不在台架进程里 import psycopg）。"""
    probe = ("import sys; import psycopg; "
             "c = psycopg.connect(sys.argv[1], connect_timeout=3); c.close(); print('ok')")
    ev = ctx.run([ctx.python, "-c", probe, dsn], timeout=60)
    return ev["rc"] == 0 and "ok" in (ev["stdout"] or "")


# ------------------------------------------------------------------ A27


def check_a27(ctx: Ctx, c: Check) -> None:
    """A27：本地回退离线命中且稳定排序；未配凭据 rc=4+中文；无 key 明文；provider=local。"""
    if not _cli_ready(ctx, c, "rag"):
        return
    if not _sub_ready(ctx, c, "rag", "search"):
        return
    db = ctx.fresh_db("a27")
    ev = ctx.init_db(db)
    c.add(cmd=ev["cmd"], rc=ev["rc"], ok=ev["rc"] == EXIT_PASS, note="A27：建临时库")

    q = "库存 校验"
    first = ctx.cli("rag", "search", q, "--json", db=db,
                    env_over={"DIFY_API_URL": "", "DIFY_API_KEY": ""})
    c.add(cmd=first["cmd"], rc=first["rc"], stdout=first["stdout"], stderr=first["stderr"],
          ok=first["rc"] == EXIT_PASS, note="A27：本地回退（无 Dify 凭据）")
    if first["rc"] != EXIT_PASS:
        c.fail(f"A27：本地回退检索 rc={first['rc']}，期望 0（离线必须可用）。")
    payload1 = last_json_line(first["stdout"])
    c.add(ok=payload1 is not None, note="A27：`rag search --json` 产出 JSON")
    if payload1 is not None:
        hits = payload1.get("hits") if isinstance(payload1, dict) else payload1
        hits = hits or []
        # 实测形状：`provider` 在**顶层**（`{"provider":"local","fallback":true,"hits":[…]}`）；
        # 逐条也允许带 provider —— 两处其一标 local 即算「降级可见」。
        top_provider = str((payload1.get("provider") if isinstance(payload1, dict) else "") or "")
        providers = {str((h or {}).get("provider")) for h in hits if isinstance(h, dict)}
        marked_local = top_provider == "local" or "local" in providers
        c.add(ok=marked_local or not hits,
              note=f"A27：命中 {len(hits)} 条；顶层 provider={top_provider!r}，"
                   f"逐条 provider 集合 = {providers or '（无）'}")
        if hits and not marked_local:
            c.fail(f"A27：回退结果没有标 provider=local（顶层={top_provider!r}，逐条={providers}）"
                   " → 会把回退伪装成 Dify。")
        fallback = payload1.get("fallback") if isinstance(payload1, dict) else None
        c.add(ok=fallback is not False,
              note=f"A27：`fallback` 标志 = {fallback!r}（不许在有回退时自称非回退）")
        second = ctx.cli("rag", "search", q, "--json", db=db,
                         env_over={"DIFY_API_URL": "", "DIFY_API_KEY": ""})
        p2 = last_json_line(second["stdout"])
        c.add(ok=p2 == payload1, note="A27：同输入两次输出逐字节一致（稳定排序）")
        if p2 != payload1:
            c.fail("A27：同输入两次检索结果不一致（排序不稳定）。")

    # 配了 URL 但缺 key → 期望 rc=4 + 中文（不是 5、不是裸崩）
    # 口径提示：D5 §3.5 说「未配置或未装依赖 → rc=4」，同时又把「本地回退」定为验收路径；
    # 实现把「URL 有、key 空」也归为「未配置」→ 回退 rc=0。台架按 **A27 验收行**（未配凭据 → rc=4）断，
    # 这条冲突已报 Chief 裁决；裁决前不放水。
    missing_key = ctx.cli("rag", "search", q, "--json", db=db,
                          env_over={"DIFY_API_URL": "https://dify.invalid/v1",
                                    "DIFY_API_KEY": ""})
    blob = (missing_key["stdout"] or "") + (missing_key["stderr"] or "")
    c.add(cmd=missing_key["cmd"], rc=missing_key["rc"], stdout=missing_key["stdout"],
          stderr=missing_key["stderr"], ok=missing_key["rc"] == EXIT_DEP and cjk(blob),
          note="A27：有 URL 无 key → rc=4 + 中文（A27 验收行；D5 §3.5 与此处不自洽，待裁）")
    if missing_key["rc"] != EXIT_DEP:
        c.fail(f"A27：缺凭据应 rc=4，实际 {missing_key['rc']}（R-31 三分）—— 实测实现把它当"
               "「未配置」直接回退 rc=0；按 A27 验收行这是口径差，等 Chief 裁「部分配置算不算意图走 Dify」。")
    if "Traceback" in blob:
        c.fail("A27：缺凭据时抛裸栈（应中文提示 + rc=4）。")

    # 不许漏 key 明文：把 key 塞进环境，逼一条失败路径，检查输出与库
    secret = "sk-SUPERSECRET-P3-9f3a"
    leak = ctx.cli("rag", "search", q, "--json", db=db, timeout=120,
                   env_over={"DIFY_API_URL": "https://dify.invalid/v1",
                             "DIFY_API_KEY": secret})
    blob2 = (leak["stdout"] or "") + (leak["stderr"] or "")
    c.add(cmd=leak["cmd"], rc=leak["rc"], stdout=leak["stdout"], stderr=leak["stderr"],
          ok=secret not in blob2, note="A27：失败路径的 stdout/stderr 里无 key 明文")
    if secret in blob2:
        c.fail("A27：输出里出现了 key 明文。")
    db_blob = ""
    try:
        for table in ("knowledge", "fastlane_requests", "ledger"):
            con = sqlite3.connect(str(db))
            try:
                rows = con.execute(f"select * from {table}").fetchall()
            except sqlite3.Error:
                rows = []
            finally:
                con.close()
            db_blob += repr(rows)
    except Exception:  # noqa: BLE001
        pass
    c.add(ok=secret not in db_blob, note="A27：库里无 key 明文")
    if secret in db_blob:
        c.fail("A27：库里落了 key 明文。")


# ------------------------------------------------------------------ 渲染与入口


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
    ("A0", "P0/P1/P2 不回归（13/13 + 8/8 + 6/6）+ pytest 全绿 + R-32 白名单覆盖率 ≥80%", "镜鉴",
     "D5 §4 A0、R-29、R-31、R-32", check_a0),
    ("A23", "快车道登记/承认落库：幂等、batch_id 同批共享、ESCALATED/REJECTED 不可翻白", "长流+磐石",
     "D5 §3.2、A23", check_a23),
    ("A24", "快车道判据与升级：三判据全真才放行；任一为假 → ESCALATED 且指名原因", "长流",
     "D5 §3.2、A24", check_a24),
    ("A25", "沉淀闸门全量：门槛丢弃 / 后续轮次才转正 / 抽检不过写回避 + 留痕", "长流",
     "D5 §3.3、A25", check_a25),
    ("A26", "负知识回灌（本轮+上一轮，R-28）+ 双库 package_hash 一致", "长流",
     "D5 §3.4、A26、R-28", check_a26),
    ("A27", "RAG：本地回退离线稳定命中 / 缺凭据 rc=4 / 无 key 明文 / provider=local", "长流",
     "D5 §3.5、A27、R-31", check_a27),
]


def _force_utf8_stdio() -> None:
    """R-19：独立脚本不经过 cli.main()，必须自己做 stdio 兜底。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(description="KFPA P3 独立验收台架（A23–A27 + A0 不回归）")
    ap.add_argument("--root", default=str(REPO_ROOT), help="仓库根")
    ap.add_argument("--only", default="", help="只跑指定断言，逗号分隔，支持前缀")
    ap.add_argument("--json", dest="json_path", default="", help="把完整证据写到 JSON")
    ap.add_argument("--coverage-out", dest="coverage_out", default="",
                    help="把 A0 的合并覆盖率报告落盘到该路径")
    ap.add_argument("--list", action="store_true", help="列出全部断言即退出")
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
        print(f"台架错误：未知断言 {unknown}；用 --list 查看。", file=sys.stderr)
        return 2
    todo = [row for row in CHECKS
            if not selected or any(row[0] == s or row[0].startswith(s + ".") for s in selected)]

    tmpdir = Path(tempfile.mkdtemp(prefix="kfpa-verify-p3-"))
    ctx = Ctx(root=root, python=sys.executable, tmp=tmpdir, coverage_out=args.coverage_out)
    print(f"KFPA P3 验收台架  仓库={root}")
    print(f"python={ctx.python}   （R-2 PYTHONPATH=src；R-19 stdio 兜底；R-25.3 剥 KFPA_DB_URL）")
    print(f"P3 契约文件缺失={[f for f in P3_FILES if not (root / f).exists()]}")
    print(f"临时目录={tmpdir}")
    print("-" * 72)

    started = time.time()
    results: list[Check] = []
    for cid, title, owner, ref, fn in todo:
        c = Check(cid=cid, title=title, owner=owner, reference=ref)
        try:
            fn(ctx, c)
        except Exception:  # noqa: BLE001
            c.fail("台架异常（断言实现自身出错）：\n" + traceback.format_exc(limit=6))
        results.append(c)
        print(render(c, args.verbose))
        print("-" * 72)

    n_pass = sum(1 for x in results if x.status == "PASS")
    n_fail = sum(1 for x in results if x.status == "FAIL")
    n_skip = sum(1 for x in results if x.status == "SKIP")
    print(f"汇总：{len(results)} 条断言 -> PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}"
          f"（{time.time() - started:.1f}s）")
    for x in results:
        if x.status != "PASS":
            print(f"  [{x.status}] {x.cid} {x.title}  (责任: {x.owner})")

    if args.json_path:
        payload = {
            "root": str(root), "python": ctx.python,
            "contract_versions_supported": list(SUPPORTED_CONTRACT_VERSIONS),
            "missing_files": [f for f in P3_FILES if not (root / f).exists()],
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

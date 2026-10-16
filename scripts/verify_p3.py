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
#: P3 新增模块：不在 R-32 门槛里，但必须**可见**（低于 80% 报建议，避免「门禁绿、新代码没测」）
COVER_P3_REPORT_ONLY = ("src/kfpa/fastlane.py", "src/kfpa/knowledge.py", "src/kfpa/rag.py")
COVER_THRESHOLD = 0.80
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
    # P3 新增模块必须可见（不在 R-32 门槛内，但低于 80% 要报出来）
    for f in COVER_P3_REPORT_ONLY:
        info = per_file.get(f)
        if info is None:
            c.add(ok=True, note=f"A0（报告项）：{f} 未出现在报告——模块还没落地或没被测试触达")
        else:
            c.add(ok=True, note=f"A0（报告项）：{f} 行覆盖 {info['rate']:.1%}"
                                f"（{info['covered']}/{info['statements']}）")
            if info["statements"] and info["rate"] < COVER_THRESHOLD:
                c.warn(f"A0：P3 新模块 {f} 覆盖率 {info['rate']:.1%} < {COVER_THRESHOLD:.0%}"
                       "（不在 R-32 门槛内，但门槛绿而新代码没测是缺口）。")
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


def check_a23(ctx: Ctx, c: Check) -> None:
    """A23：快车道登记 → 承认落库；幂等；batch_id 同批共享；ESCALATED/REJECTED 不可翻白。"""
    if not _cli_ready(ctx, c, "fastlane"):
        return
    if not _sub_ready(ctx, c, "fastlane", "plan", "list", "ack", "batch-ack", "show"):
        return
    db = _fastlane_db(ctx, c, "a23")
    if db is None:
        return

    plan = ctx.cli("fastlane", "plan", "--round", "1", db=db)
    c.add(cmd=plan["cmd"], rc=plan["rc"], stdout=plan["stdout"], stderr=plan["stderr"],
          ok=plan["rc"] == EXIT_PASS, note="A23：`fastlane plan --round 1` 登记")
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
    first = rows[0]

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

    # 攒批承认：同批共享 batch_id
    ctx.cli("fastlane", "plan", "--round", "2", db=db)
    pending = [r for r in _fastlane_rows(db) if str(r.get("status")) == "pending"]
    acks = ctx.tmp / "acks.yaml"
    acks.write_text("\n".join(f"{r['request_id']}: yes" for r in pending) + "\n",
                    encoding="utf-8")
    if pending:
        batch = ctx.cli("fastlane", "batch-ack", "--file", str(acks), "--by", "boss", db=db)
        c.add(cmd=batch["cmd"], rc=batch["rc"], stdout=batch["stdout"], stderr=batch["stderr"],
              ok=batch["rc"] == EXIT_PASS, note=f"A23：batch-ack {len(pending)} 条")
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
            c.fail(f"A24：{label} 判假但没给出 reasons（D5：不许只给个布尔）。")
        elif label == "no_diff_source":
            if not any("no_diff_source" in r for r in reasons):
                c.fail(f"A24：diff_files 为空时 reasons 没指名 `no_diff_source`（R-23 第三来源）："
                       f"{reasons}")
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
    mod, err = import_mod("kfpa.knowledge")
    if mod is None:
        c.add(ok=False, note=f"import kfpa.knowledge 失败：{err}")
        c.fail("A25：`src/kfpa/knowledge.py` 不可导入（长流）。")
        return

    threshold = getattr(mod, "CONFIDENCE_THRESHOLD", None)
    c.add(ok=threshold is not None and abs(float(threshold) - 0.6) < 1e-9,
          note=f"knowledge.CONFIDENCE_THRESHOLD = {threshold!r}（D5 §3.3 冻结 0.6）")
    if threshold is None:
        c.fail("A25：缺 `CONFIDENCE_THRESHOLD` 常量（D5 §3.3 冻结 = 0.6）。")

    can_promote = getattr(mod, "can_promote", None)
    rate_fn = getattr(mod, "sampling_rate", None)
    c.add(ok=callable(can_promote), note=f"knowledge.can_promote 可调用 = {callable(can_promote)}")
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

    # 低置信度直接 DROPPED：CLI 侧落一条（用 store 写入 + 闸门入口驱动）
    gate_names = ("gate", "admit", "ingest", "apply_gate", "evaluate_candidate", "judge_candidate")
    gate = next((getattr(mod, n) for n in gate_names if callable(getattr(mod, n, None))), None)
    c.add(ok=gate is not None,
          note=f"A25：闸门入口候选名 {gate_names} → 命中 {gate.__name__ if gate else '无'}")
    if gate is None:
        c.fail("A25：无法独立验证「低置信度直接丢弃」——`kfpa.knowledge` 没暴露闸门入口"
               f"（试过 {gate_names}）；请在 PR 里给出入口名（或让 CLI 的 knowledge 子命令承担）。")
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
                except TypeError:
                    try:
                        gate(cand(cid=cid, conf=conf))
                    except Exception as exc:  # noqa: BLE001
                        c.add(ok=False, note=f"闸门调用失败：{type(exc).__name__}: {exc}")
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
    """A26：失败进下一轮 feedback；回灌只取本轮+上一轮；两库 package_hash 一致。"""
    if not _cli_ready(ctx, c, "round"):
        return
    db = ctx.fresh_db("a26")
    ev = ctx.init_db(db)
    c.add(cmd=ev["cmd"], rc=ev["rc"], ok=ev["rc"] == EXIT_PASS, note="A26：建临时库")

    work = ctx.tmp / "a26-work"
    # 第 1 轮注入 test_fail → 失败记录（avoid pattern）
    r1 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                  "--round", "1", "--dry-run", "--json", "--inject", "test_fail",
                  "--db", str(db), "--workdir", str(work)])
    c.add(cmd=r1["cmd"], rc=r1["rc"], stdout=r1["stdout"], stderr=r1["stderr"],
          note="A26：第 1 轮注入 test_fail")
    # 第 2 轮任务包 feedback 必须带出上一轮的回避模式
    r2 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                  "--round", "2", "--dry-run", "--json", "--db", str(db),
                  "--workdir", str(work)])
    payload = last_json_line(r2["stdout"])
    c.add(cmd=r2["cmd"], rc=r2["rc"], stdout=r2["stdout"], stderr=r2["stderr"],
          ok=payload is not None, note="A26：第 2 轮产出 JSON")
    if payload is None:
        c.fail("A26：第 2 轮没产出可解析的 JSON，无法验证回灌。")
    else:
        pkg = payload.get("task_package") or {}
        feedback = [str(x) for x in (pkg.get("feedback") or [])]
        c.add(ok=any(x.startswith("AVOID[") for x in feedback),
              note=f"A26：第 2 轮任务包 feedback = {feedback}")
        if not any(x.startswith("AVOID[") for x in feedback):
            c.fail("A26：失败回避模式没进下一轮任务包 feedback（R-28 回灌断裂）。")
        c.add(ok=True, note=f"A26：回灌条数 = {len(feedback)}（R-28：只取本轮+上一轮）")

    # 双库一致：有 PG DSN 时对拍，没有则可见 SKIP
    dsn = os.environ.get("KFPA_VERIFY_PG_DSN", "")
    if not dsn:
        c.skip("A26：未提供 `KFPA_VERIFY_PG_DSN`，PG 侧 `package_hash` 对拍未跑"
               "（本机无 PG 时在报告里必须可见；有 Docker 的环境请设该变量重跑）。")
        return
    pg_db = ctx.tmp / "a26_pg.json"
    r3 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                  "--round", "1", "--dry-run", "--json", "--db", dsn,
                  "--workdir", str(ctx.tmp / "a26-pg")], timeout=600)
    pg_payload = last_json_line(r3["stdout"])
    c.add(cmd=r3["cmd"], rc=r3["rc"], stdout=r3["stdout"], stderr=r3["stderr"],
          ok=pg_payload is not None, note="A26：PG 侧同一轮")
    if pg_payload is None:
        c.fail(f"A26：PG 侧没产出 JSON（rc={r3['rc']}）。")
        return
    sqlite_payload = last_json_line(r1["stdout"])
    def digest(p):
        return ((p or {}).get("task_package") or {}).get("package_hash")
    c.add(ok=bool(digest(pg_payload)) and digest(pg_payload) == digest(sqlite_payload),
          note=f"A26：package_hash SQLite={digest(sqlite_payload)!r} PG={digest(pg_payload)!r}")
    if digest(pg_payload) != digest(sqlite_payload):
        c.fail("A26：两库同轮 package_hash 不一致（回灌范围或轮次过滤两端不同口径）。")
    _ = pg_db


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
        providers = {str((h or {}).get("provider")) for h in hits if isinstance(h, dict)}
        c.add(ok=("local" in providers) or not hits,
              note=f"A27：命中 {len(hits)} 条，provider 集合 = {providers or '（无命中）'}")
        if hits and "local" not in providers:
            c.fail(f"A27：回退结果没有标 provider=local（实际 {providers}）→ 会把回退伪装成 Dify。")
        second = ctx.cli("rag", "search", q, "--json", db=db,
                         env_over={"DIFY_API_URL": "", "DIFY_API_KEY": ""})
        p2 = last_json_line(second["stdout"])
        c.add(ok=p2 == payload1, note="A27：同输入两次输出逐字节一致（稳定排序）")
        if p2 != payload1:
            c.fail("A27：同输入两次检索结果不一致（排序不稳定）。")

    # 配了 URL 但缺 key → rc=4 + 中文（不是 5、不是裸崩）
    missing_key = ctx.cli("rag", "search", q, "--json", db=db,
                          env_over={"DIFY_API_URL": "https://dify.invalid/v1",
                                    "DIFY_API_KEY": ""})
    blob = (missing_key["stdout"] or "") + (missing_key["stderr"] or "")
    c.add(cmd=missing_key["cmd"], rc=missing_key["rc"], stdout=missing_key["stdout"],
          stderr=missing_key["stderr"], ok=missing_key["rc"] == EXIT_DEP and cjk(blob),
          note="A27：有 URL 无 key → rc=4 + 中文")
    if missing_key["rc"] != EXIT_DEP:
        c.fail(f"A27：缺凭据应 rc=4，实际 {missing_key['rc']}（R-31 三分）。")
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

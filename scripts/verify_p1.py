#!/usr/bin/env python
"""KFPA P1 独立验收台架（A11–A17，另加 A0 = P0 不回归守卫）。

用法（R-2：统一 PYTHONPATH=src）：
    python scripts/verify_p1.py                    # 全量
    python scripts/verify_p1.py --only A12,A14     # 只跑指定断言（支持前缀）
    python scripts/verify_p1.py --json out.json    # 落机器可读证据
    python scripts/verify_p1.py --list
    python scripts/verify_p1.py --skip-pg          # 跳过需要 Docker 的 A12（本机无 Docker 时）

依据：docs/dev/p1-plan.md（D3）§3 冻结接口 + §4 验收 A11–A17 + §6 合并门禁。

设计原则（与 verify_p0.py 一致；R-19：本脚本自己做 stdio 兜底）：
1. 断言之间独立：各自建临时库/临时 workdir，不依赖执行顺序。
2. 模块缺失 != 通过：按 FAIL 报，并指名缺哪个文件、归谁写。
3. 证据 = 真实命令 + 真实退出码 + 真实输出片段。
4. clean env 优先：A10 那类缺陷必须在不设 PYTHONIOENCODING 时也测得到。
5. A12 需要 Docker：本机没有 docker / compose 时显式 SKIP（可见，不算通过）。

退出码：0=无 FAIL（SKIP 不影响）；1=有 FAIL；2=台架自身用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: D3 §3.1：P1 用契约 1.2
CONTRACT_VERSION_LATEST = "1.2"
SUPPORTED_CONTRACT_VERSIONS = ("1.2",)

EXIT_PASS, EXIT_USAGE, EXIT_CONFIRM, EXIT_DEP, EXIT_IO, EXIT_NEEDS_FIX, EXIT_BLOCKED = \
    0, 2, 3, 4, 5, 10, 11

#: D3 §2：P1 增量文件 -> 唯一写者
P1_FILES = {
    "docker-compose.pg.yml": "磐石",
    "migrations/0001_init.sql": "磐石",
    "src/kfpa/store/__init__.py": "磐石",
    "src/kfpa/store/sqlite.py": "磐石",
    "src/kfpa/store/postgres.py": "磐石",
    "src/kfpa/ledger.py": "长流",
    "src/kfpa/knowledge.py": "长流",
    "src/kfpa/impact.py": "长流",
    "src/kfpa/ui/fastlane.py": "界面",
    "src/kfpa/ui/streamlit_app.py": "界面",
}

PG_COMPOSE = "docker-compose.pg.yml"
PG_PORT = "55432"

#: P0 七表 + P1 两表（D3 §3.2）
P1_TABLES = ("rounds", "tickets", "task_packages", "gate_reports", "failures",
             "snapshots", "meta", "ledger", "knowledge")


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
    skip_pg: bool = False

    def __post_init__(self) -> None:
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(self.root / "src") + os.pathsep + self.env.get("PYTHONPATH", "")
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["PYTHONUTF8"] = "1"
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
        except FileNotFoundError as exc:
            return {"cmd": " ".join(cmd), "rc": -8, "stdout": "", "stderr": f"命令不存在：{exc}"}
        return {"cmd": " ".join(cmd), "rc": p.returncode,
                "stdout": p.stdout.decode("utf-8", "replace"),
                "stderr": p.stderr.decode("utf-8", "replace")}

    def fresh_db(self, name: str) -> Path:
        p = self.tmp / f"{name}.db"
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
        return p

    def design(self) -> Path:
        return self.root / "examples/design-r1.md"


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


def status_value(status: object) -> str:
    return str(getattr(status, "value", status)).strip().lower()


def missing_hint(ctx: Ctx, cid: str, extra: tuple = ()) -> str:
    missing = [f for f in (*P1_FILES, *extra) if not (ctx.root / f).exists()]
    if not missing:
        return f"{cid}：P1 契约文件齐备，但行为不满足断言 —— 见上述命令证据。"
    lines = [f"{cid} 无法验收：缺 {len(missing)} 个文件（D3 §2）："]
    lines += [f"    - {f}    <- {P1_FILES.get(f, '未登记')}" for f in missing]
    return "\n".join(lines)


def note_missing(ctx: Ctx, c: Check, only_owners: tuple = ()) -> None:
    """把「缺哪些 P1 文件、归谁」写进 findings，让失败能直接定位到人。"""
    rows = []
    for f, owner in P1_FILES.items():
        if only_owners and owner not in only_owners:
            continue
        if not (ctx.root / f).exists():
            rows.append(f"{f}（{owner}）")
    if rows:
        c.findings.append("[缺件] " + "、".join(rows))


def last_json_line(blob: str):
    for line in reversed((blob or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return None


def probe_sqlite(db: Path):
    """原始读表名/行数（不经被测代码）。"""
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


def pg_dsn(ctx: Ctx) -> tuple[str, str]:
    """从 compose 文件读 DSN；缺文件/读不出时给默认值并说明。"""
    compose = ctx.root / PG_COMPOSE
    user, pwd, db = "postgres", "postgres", "kfpa"
    err = ""
    if compose.exists():
        try:
            import yaml
            spec = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
            services = spec.get("services") or {}
            for svc in services.values():
                env = svc.get("environment") or {}
                if isinstance(env, list):
                    env = dict(x.split("=", 1) for x in env if "=" in x)
                user = env.get("POSTGRES_USER", user)
                pwd = env.get("POSTGRES_PASSWORD", pwd)
                db = env.get("POSTGRES_DB", db)
                break
        except Exception as exc:  # noqa: BLE001
            err = f"（compose 解析失败，用默认值：{type(exc).__name__}: {exc}）"
    return f"postgresql://{user}:{pwd}@127.0.0.1:{PG_PORT}/{db}", err


PG_PROBE = r'''
import json, sys
import psycopg
dsn = sys.argv[1]
out = {}
with psycopg.connect(dsn, connect_timeout=5) as conn:
    with conn.cursor() as cur:
        cur.execute("select table_name from information_schema.tables where table_schema='public'")
        out["tables"] = sorted(r[0] for r in cur.fetchall())
        counts = {}
        for t in out["tables"]:
            cur.execute('select count(*) from "%s"' % t)
            counts[t] = cur.fetchone()[0]
        out["counts"] = counts
        for key, sql in (("tickets", "select ticket_id from tickets order by ticket_id"),
                         ("gates", "select state from gate_reports order by id"),
                         ("snapshots", "select round_no, package_hash, gate_state, tag from snapshots order by round_no"),
                         ("ledger_roles", "select role, count(*) from ledger group by role order by role")):
            try:
                cur.execute(sql)
                out[key] = [list(r) if isinstance(r, tuple) else r for r in cur.fetchall()]
            except Exception:
                out[key] = None
print(json.dumps(out, ensure_ascii=False))
'''


# ------------------------------------------------------------------ A0


def check_a0(ctx: Ctx, c: Check) -> None:
    """D3 §6.2：每合一步，P0 台架不许回归（13/13）；顺带守契约版本（R-20）。"""
    sc, serr = import_mod("kfpa.schemas")
    if sc is None:
        c.add(ok=False, note=f"import kfpa.schemas 失败：{serr}")
        c.fail("A0：契约不可导入。")
    else:
        ver = str(getattr(sc, "CONTRACT_VERSION", ""))
        c.add(ok=ver in SUPPORTED_CONTRACT_VERSIONS,
              note=f"契约版本 = {ver!r}；P1 台架对齐 {SUPPORTED_CONTRACT_VERSIONS}（R-20 升版守卫）")
        if ver not in SUPPORTED_CONTRACT_VERSIONS:
            c.fail(f"A0：契约版本 {ver} 未同步到 P1 台架 —— 升版必须同批更新 "
                   f"scripts/verify_p1.py 的 SUPPORTED_CONTRACT_VERSIONS。")

    p0 = ctx.root / "scripts/verify_p0.py"
    if not p0.exists():
        c.fail("缺 scripts/verify_p0.py —— P0 门禁不见了。")
        return
    out = ctx.tmp / "p0-regression.json"
    ev = ctx.run([ctx.python, str(p0), "--json", str(out)], clean_env=True)
    c.add(cmd=ev["cmd"] + "    [clean-env]", rc=ev["rc"], stdout=ev["stdout"],
          stderr=ev["stderr"], ok=ev["rc"] == 0 and out.exists(),
          note="A0：P0 台架 clean env 全量必须 rc=0（D3 §6.2）")
    if not out.exists():
        c.fail(f"A0：P0 台架没产出证据文件（rc={ev['rc']}）。")
        return
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        c.fail(f"A0：P0 证据 JSON 无法解析：{exc}")
        return
    s = data.get("summary", {})
    c.add(ok=s.get("fail") == 0 and s.get("total", 0) >= 13,
          note=f"A0：P0 台架汇总 = {s}")
    if s.get("fail") != 0 or s.get("total", 0) < 13:
        bad = [x["cid"] for x in data.get("checks", []) if x.get("status") != "PASS"]
        c.fail(f"A0：P0 台架回归 —— 非 PASS = {bad}，汇总 = {s}。")


# ------------------------------------------------------------------ A11


def check_a11(ctx: Ctx, c: Check) -> None:
    """A11：token 账本离线可用（D3 §3.4）。"""
    note_missing(ctx, c, ("长流", "磐石"))
    db = ctx.fresh_db("a11")
    work = ctx.tmp / "a11-work"
    work.mkdir(parents=True, exist_ok=True)
    ev0 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    c.add(cmd=ev0["cmd"], rc=ev0["rc"], ok=ev0["rc"] == EXIT_PASS, note="A11 前置：建库")
    ev1 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "1", "--dry-run", "--json", "--db", str(db),
                   "--workdir", str(work)])
    c.add(cmd=ev1["cmd"], rc=ev1["rc"], stdout=ev1["stdout"], stderr=ev1["stderr"],
          ok=ev1["rc"] == EXIT_PASS, note="A11：mock 跑一轮（离线）")
    if ev1["rc"] != EXIT_PASS:
        c.fail(missing_hint(ctx, "A11"))

    tables, counts, err = probe_sqlite(db)
    n = counts.get("ledger", -1)
    c.add(ok=n > 0, note=f"`ledger` 表行数 = {n}" + (f"；读取错误 {err}" if err else ""))
    if n <= 0:
        c.fail("A11：一轮之后 `ledger` 表无记录（§3.4：每次 LLM.complete 必须产生一条 LedgerEntry）。")

    ev2 = ctx.run([ctx.python, "-m", "kfpa", "ledger", "--round", "1", "--db", str(db)])
    blob = ev2["stdout"] + ev2["stderr"]
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] == EXIT_PASS and bool(blob.strip()),
          note="A11：`kfpa ledger --round 1` 期望 rc=0 且列出每角色与合计")
    if ev2["rc"] != EXIT_PASS:
        c.fail("A11：`kfpa ledger` 不可用（§3.4 CLI）。")
    for key in ("ARCHITECT", "FLASH"):
        c.add(ok=key in blob, note=f"ledger 输出含角色 {key}")

    ev3 = ctx.run([ctx.python, "-m", "kfpa", "ledger", "--round", "1", "--json", "--db", str(db)])
    data = last_json_line(ev3["stdout"])
    c.add(cmd=ev3["cmd"], rc=ev3["rc"], ok=ev3["rc"] == EXIT_PASS and data is not None,
          note="A11：`--json` 末行可解析（R-19.4：取最后一行）")
    if data is not None:
        joined = json.dumps(data, ensure_ascii=False)
        c.add(ok="total" in joined.lower(), note=f"ledger --json 含合计字段：{joined[:200]}")

    # model_ref 必须来自 config（不写死）；estimate_tokens 确定性
    ledger_mod, lerr = import_mod("kfpa.ledger")
    if ledger_mod is None:
        c.add(ok=False, note=f"import kfpa.ledger 失败：{lerr}")
        c.fail("A11：`src/kfpa/ledger.py` 不可导入（长流）。")
    else:
        fn = getattr(ledger_mod, "estimate_tokens", None)
        if fn is None:
            c.fail("A11：`ledger.estimate_tokens()` 不存在（§3.4）。")
        else:
            got = fn("abcdefghij")   # 10 字符 // 3 = 3
            c.add(ok=got == 3, note=f"estimate_tokens('abcdefghij') = {got}（期望 3 = 字符数//3）")
            if got != 3:
                c.fail("A11：`estimate_tokens` 不符合确定性口径（字符数 // 3）。")
    try:
        store_mod = import_mod("kfpa.store")[0]
        if store_mod is not None and hasattr(store_mod, "make_store"):
            st = store_mod.make_store(str(db))
            entries = st.list_ledger(1)
            roles = {e.role for e in entries}
            models = {e.model_ref for e in entries}
            c.add(ok=bool(entries), note=f"list_ledger(1) = {len(entries)} 条，角色 {sorted(roles)}")
            c.add(ok=bool(models) and all(m for m in models),
                  note=f"model_ref 取值 = {sorted(models)}（须来自 config/models.yaml，不得写死）")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"list_ledger 异常：{type(exc).__name__}: {exc}")
        c.fail("A11：Store.list_ledger 接口不可用（§3.2）。")


# ------------------------------------------------------------------ A12


def check_a12(ctx: Ctx, c: Check) -> None:
    """A12：双库对拍（SQLite vs PostgreSQL）。"""
    compose = ctx.root / PG_COMPOSE
    if not compose.exists():
        c.fail(f"A12 无法验收：缺 {PG_COMPOSE}（磐石，D3 §2）。")
        return
    if ctx.skip_pg:
        c.skip("--skip-pg：本机不跑 Docker，A12 的双库对拍未执行。")
        return
    if shutil.which("docker") is None:
        c.skip("本机无 docker CLI，A12 需要 Docker + postgres:16-alpine，未执行。")
        return

    # 先记录「本来是否已起」——已起则不负责关
    pre = ctx.run(["docker", "compose", "-f", str(compose), "ps", "-q"], timeout=60)
    started_here = not (pre["stdout"] or "").strip()

    up = ctx.run(["docker", "compose", "-f", str(compose), "up", "-d", "--wait"],
                 timeout=420)
    c.add(cmd=up["cmd"], rc=up["rc"], stdout=up["stdout"], stderr=up["stderr"],
          ok=up["rc"] == 0, note="A12：起 PG（compose，端口 55432）")
    if up["rc"] != 0:
        c.fail("A12：`docker compose up -d --wait` 失败 —— 见上方 stderr（镜像/端口/健康检查归磐石）。")
        return

    dsn, derr = pg_dsn(ctx)
    if derr:
        c.warn(f"A12：{derr}")
    masked = re.sub(r"://[^@]+@", "://***@", dsn)
    c.add(ok=True, note=f"A12：DSN（打码）= {masked}")

    env = {"KFPA_DB_URL": dsn}
    pg_db = ctx.tmp / "a12-pg"          # 仅为传递 --db 占位；PG 侧由 KFPA_DB_URL 决定
    try:
        ev0 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(pg_db)],
                      env_over=env)
        c.add(cmd=ev0["cmd"] + "    [KFPA_DB_URL=***]", rc=ev0["rc"], stdout=ev0["stdout"],
              stderr=ev0["stderr"], ok=ev0["rc"] == EXIT_PASS, note="A12：PG 侧建库")
        if ev0["rc"] != EXIT_PASS:
            c.fail("A12：PG 侧 `kfpa init` 失败（§3.2 双驱动）。")
            return
        work_pg = ctx.tmp / "a12-pg-work"
        work_pg.mkdir(parents=True, exist_ok=True)
        ev1 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                       "--round", "1", "--dry-run", "--json", "--db", str(pg_db),
                       "--workdir", str(work_pg)], env_over=env)
        c.add(cmd=ev1["cmd"] + "    [KFPA_DB_URL=***]", rc=ev1["rc"], stdout=ev1["stdout"],
              stderr=ev1["stderr"], ok=ev1["rc"] == EXIT_PASS,
              note="A12：PG 侧跑 A2 同一命令")
        if ev1["rc"] != EXIT_PASS:
            c.fail("A12：PG 侧 round 未通过（与 SQLite 不一致）。")

        probe = ctx.run([ctx.python, "-c", PG_PROBE, dsn], timeout=120)
        c.add(cmd="python -c <PG 探针>", rc=probe["rc"], stdout=probe["stdout"],
              stderr=probe["stderr"], ok=probe["rc"] == 0, note="A12：原始读 PG 表结构")
        pg = last_json_line(probe["stdout"]) if probe["rc"] == 0 else None
        if pg is None:
            c.fail(f"A12：PG 探针失败：{excerpt(probe['stderr'], 300)}")
        else:
            missing = [t for t in P1_TABLES if t not in set(pg.get("tables") or [])]
            c.add(ok=not missing, note=f"A12：PG 表 = {pg.get('tables')}；缺 {missing or '无'}")
            if missing:
                c.fail(f"A12：PG 缺表 {missing}（D3 §3.2：P0 七表 + ledger + knowledge）。")
    finally:
        if started_here:
            down = ctx.run(["docker", "compose", "-f", str(compose), "down", "-v"], timeout=180)
            c.add(cmd=down["cmd"], rc=down["rc"], ok=down["rc"] == 0,
                  note="A12：收尾（本次由台架起的栈，用完 down -v）")

    # SQLite 侧同命令 + 对拍
    db = ctx.fresh_db("a12-sqlite")
    sq_work = ctx.tmp / "a12-sqlite-work"
    sq_work.mkdir(parents=True, exist_ok=True)
    ev2 = ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    ev3 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "1", "--dry-run", "--json", "--db", str(db),
                   "--workdir", str(sq_work)])
    c.add(cmd=ev3["cmd"], rc=ev3["rc"], stdout=ev3["stdout"], stderr=ev3["stderr"],
          ok=ev2["rc"] == EXIT_PASS and ev3["rc"] == EXIT_PASS, note="A12：SQLite 侧同命令")
    tables, counts, err = probe_sqlite(db)
    sg = last_json_line(ev3["stdout"]) or {}
    c.add(ok=not err, note=f"A12：SQLite 表 = {sorted(tables)}")
    if pg is not None:
        sq_tickets = sorted(r[0] for r in sqlite3.connect(str(db)).execute(
            "select ticket_id from tickets").fetchall()) if "tickets" in tables else []
        pg_tickets = sorted(pg.get("tickets") or [])
        c.add(ok=sq_tickets == pg_tickets,
              note=f"A12：票号一致 SQLite={sq_tickets} PG={pg_tickets}")
        if sq_tickets != pg_tickets:
            c.fail("A12：两库票号不一致（双驱动行为必须一致，D3 §3.2）。")
        sq_snaps = sqlite3.connect(str(db)).execute(
            "select round_no, package_hash, gate_state, tag from snapshots order by round_no"
        ).fetchall() if "snapshots" in tables else []
        pg_snaps = [tuple(x) for x in (pg.get("snapshots") or [])]
        c.add(ok=[tuple(x) for x in sq_snaps] == pg_snaps,
              note=f"A12：快照一致 SQLite={sq_snaps} PG={pg_snaps}")
        if [tuple(x) for x in sq_snaps] != pg_snaps:
            c.fail("A12：两库快照（package_hash/gate_state/tag）不一致。")


# ------------------------------------------------------------------ A13


def check_a13(ctx: Ctx, c: Check) -> None:
    """A13：checkpoint 落盘 + 同轮重跑幂等（D3 §3.5）。"""
    note_missing(ctx, c, ("长流",))
    db = ctx.fresh_db("a13")
    work = ctx.tmp / "a13-work"
    work.mkdir(parents=True, exist_ok=True)
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    base = [ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
            "--round", "1", "--dry-run", "--json", "--db", str(db), "--workdir", str(work)]
    ev1 = ctx.run(base)
    c.add(cmd=ev1["cmd"], rc=ev1["rc"], stdout=ev1["stdout"], stderr=ev1["stderr"],
          ok=ev1["rc"] == EXIT_PASS, note="A13：第 1 次跑第 1 轮")
    rr1 = last_json_line(ev1["stdout"]) or {}
    ids1 = sorted(t.get("ticket_id", "") for t in (rr1.get("tickets") or []))
    c.add(ok=bool(ids1), note=f"第 1 次的票号 = {ids1}")

    # 关一张票，验证重跑保留状态（R-6）
    if ids1:
        ec = ctx.run([ctx.python, "-m", "kfpa", "tickets", "close", ids1[0],
                      "--answer", "选 A：按仓隔离", "--db", str(db)])
        c.add(cmd=ec["cmd"], rc=ec["rc"], ok=ec["rc"] == EXIT_PASS,
              note=f"A13：关票 {ids1[0]}（为 R-6 保留性做前置）")

    ev2 = ctx.run(base)
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] in (EXIT_PASS, EXIT_NEEDS_FIX, EXIT_BLOCKED),
          note="A13：同轮重跑")
    rr2 = last_json_line(ev2["stdout"]) or {}
    ids2 = sorted(t.get("ticket_id", "") for t in (rr2.get("tickets") or []))
    c.add(ok=ids1 == ids2, note=f"重跑票号集合一致 = {ids1 == ids2}（{ids2}）")
    if ids1 and ids1 != ids2:
        c.fail(f"A13：同轮重跑票号集合变了（{(set(ids2) - set(ids1)) or set(ids1) - set(ids2)}）——"
               "checkpoint 没复用、重复开票。")
    if ids1:
        try:
            con = sqlite3.connect(str(db))
            row = con.execute("select status, answer from tickets where ticket_id = ?",
                              (ids1[0],)).fetchone()
            con.close()
            c.add(ok=bool(row) and str(row[0]).lower().startswith("clos") and bool((row[1] or "").strip()),
                  note=f"A13/R-6：重跑后 {ids1[0]} = {row}")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"读票失败：{exc}")

    # checkpoint 文件与 thread_id
    cp_candidates = []
    for base_dir in (work, ctx.root):
        for pat in ("*.sqlite", "*.sqlite3", "*.db", "*.checkpoints"):
            cp_candidates += [p for p in base_dir.glob(pat)
                              if p.name not in (db.name,) and "kfpa.db" != p.name]
    found = []
    for p in cp_candidates:
        try:
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in con.execute(
                    "select name from sqlite_master where type='table'").fetchall()}
                if "checkpoints" in tables:
                    threads = sorted({r[0] for r in con.execute(
                        "select distinct thread_id from checkpoints").fetchall()})
                    found.append((p.name, threads))
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            continue
    c.add(ok=bool(found), note=f"A13：找到 checkpoint 库 = {found or '无'}")
    if not found:
        c.fail("A13：没找到 SqliteSaver 落盘的 checkpoint 库（§3.5：P1 默认 SqliteSaver）。")
    else:
        threads = {t for _, ts in found for t in ts}
        c.add(ok="kfpa-r1" in threads,
              note=f"A13：checkpoint thread_id = {sorted(threads)}（期望含 `kfpa-r1`）")
        if "kfpa-r1" not in threads:
            c.fail(f"A13：thread_id 应为 `kfpa-r1`，实际 {sorted(threads)}。")

    try:
        import importlib.util as ilu
        has_saver = ilu.find_spec("langgraph.checkpoint.sqlite") is not None
        c.add(ok=has_saver, note="langgraph.checkpoint.sqlite 可导入（SqliteSaver 依赖）")
        if not has_saver:
            c.fail("A13：缺 `langgraph.checkpoint.sqlite`（langgraph-checkpoint-sqlite）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"检查 SqliteSaver 依赖异常：{exc}")


# ------------------------------------------------------------------ A14


def check_a14(ctx: Ctx, c: Check) -> None:
    """A14：G7 覆盖率门槛（D3 §3.3）。"""
    sc, serr = import_mod("kfpa.schemas")
    gates_mod, gerr = import_mod("kfpa.gates")
    if sc is None or gates_mod is None:
        c.add(ok=False, note=f"导入失败：schemas={serr} gates={gerr}")
        c.fail(missing_hint(ctx, "A14"))
        return
    CR = getattr(sc, "CoverageReport", None)
    if CR is None:
        c.fail("A14：契约 1.2 的 `CoverageReport` 不存在（schemas.py）。")
        return

    # 阈值语义（纯函数）
    cases = [
        ("0.79 / 关键路径绿", dict(line_rate=0.79, files={"a.py": 0.79}, critical_paths_ok=True), False),
        ("0.80 / 关键路径绿", dict(line_rate=0.80, files={"a.py": 0.80}, critical_paths_ok=True), True),
        ("0.95 / 关键路径红", dict(line_rate=0.95, files={"a.py": 0.95}, critical_paths_ok=False), False),
    ]
    for label, kwargs, expect in cases:
        try:
            rep = CR(**kwargs)
            got = rep.passed()
            c.add(ok=got is expect, note=f"CoverageReport.passed() {label} -> {got}（期望 {expect}）")
            if got is not expect:
                c.fail(f"A14：`CoverageReport.passed()` 判据错（{label}）。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"CoverageReport({label}) 异常：{type(exc).__name__}: {exc}")
            c.fail("A14：CoverageReport 构造/判据不可用。")

    # 门禁：覆盖率不达标 → NEEDS_FIX + error_category=coverage
    try:
        store_mod, _ = import_mod("kfpa.store")
        st = store_mod.make_store(":memory:") if store_mod and hasattr(store_mod, "make_store") \
            else store_mod.Store(":memory:")
        pkg = sc.TaskPackage(
            round_no=91, feature="f", goal="g",
            acceptance=[sc.AcceptanceCriterion(cid="AC-1", text="t")],
            boundaries=["b"], interface_contract=["i"], prohibitions=["p"], feedback=[])
        ex = sc.ExecResult(ok=True, results={"AC-1": True}, executor="mock")
        rep = gates_mod.evaluate(91, pkg, ex, st, attempt=1,
                                 coverage=CR(line_rate=0.5, files={"a.py": 0.5},
                                             critical_paths_ok=True))
        c.add(ok=rep.state.value == "needs_fix",
              note=f"G7：覆盖率 0.50 -> state={rep.state.value}（期望 needs_fix）")
        if rep.state.value != "needs_fix":
            c.fail("A14：G7 覆盖率不达标未判 NEEDS_FIX。")
        key = getattr(rep, "reason_key", "") or ""
        c.add(ok=("coverage" in " ".join(rep.reasons or [])) or bool(key),
              note=f"G7 reasons={rep.reasons} reason_key={key!r}")
        c.add(ok=any("a.py" in str(x) for x in (rep.failing_criteria or [])),
              note=f"G7 failing_criteria={rep.failing_criteria}（应含未达标文件 a.py）")
        if not any("a.py" in str(x) for x in (rep.failing_criteria or [])):
            c.fail("A14：G7 未把未达标文件写进 `failing_criteria`（§3.3）。")
    except TypeError as exc:
        c.add(ok=False, note=f"gates.evaluate 不接受 coverage 参数：{exc}")
        c.fail("A14：`gates.evaluate(..., coverage=CoverageReport)` 接口未实现（D3 §3.3）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"G7 用例异常：{type(exc).__name__}: {exc}")
        c.fail("A14：G7 覆盖率门槛不可判定。")

    # 真实覆盖率报告（记录为主；低于 0.80 给建议，不单独判红）
    # COVERAGE_FILE 指到临时目录：不在仓库根留 .coverage 之类的产物
    cov_json = ctx.tmp / "coverage.json"
    ev = ctx.run([ctx.python, "-m", "pytest", "--cov=kfpa", "--cov-report=json:" + str(cov_json),
                  "-q"], timeout=900,
                 env_over={"COVERAGE_FILE": str(ctx.tmp / ".coverage")})
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] in (0, 1) and cov_json.exists(),
          note="A14：`pytest --cov=kfpa --cov-report=json` 产出报告")
    if not cov_json.exists():
        c.fail(f"A14：未产出覆盖率 JSON（rc={ev['rc']}）—— 缺 pytest-cov？")
    else:
        try:
            total = json.loads(cov_json.read_text(encoding="utf-8")).get("totals", {})
            rate = float(total.get("percent_covered", 0)) / 100.0
            c.add(ok=True, note=f"A14：当前仓库行覆盖率 = {rate:.1%}（门槛 0.80）")
            if rate < 0.80:
                c.warn(f"仓库自身行覆盖率 {rate:.1%} < G7 门槛 80%：P1 收口前需补测试或用 "
                       "`--cov` 排除清单收敛（本条是建议，不单独判红）。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"覆盖率 JSON 解析失败：{exc}")


# ------------------------------------------------------------------ A15


def check_a15(ctx: Ctx, c: Check) -> None:
    """A15：沉淀闸门（D3 §3.5）。"""
    note_missing(ctx, c, ("长流", "磐石"))
    sc, serr = import_mod("kfpa.schemas")
    kn, kerr = import_mod("kfpa.knowledge")
    store_mod, ster = import_mod("kfpa.store")
    if sc is None or kn is None or store_mod is None:
        c.add(ok=False, note=f"导入失败：schemas={serr} knowledge={kerr} store={ster}")
        c.fail(missing_hint(ctx, "A15"))
        return
    KC = getattr(sc, "KnowledgeCandidate", None)
    if KC is None:
        c.fail("A15：契约 1.2 的 `KnowledgeCandidate` 不存在（schemas.py）。")
        return
    st = store_mod.make_store(":memory:") if hasattr(store_mod, "make_store") \
        else store_mod.Store(":memory:")

    def try_call(*names):
        for n in names:
            fn = getattr(kn, n, None)
            if callable(fn):
                return fn
        return None

    # 入隔离区规则：<0.80 直接 dropped；>=0.80 → quarantine
    low = KC(cid="K-LOW", round_no=1, kind="success", content="x", confidence=0.79,
             status="quarantine", reused_by=[])
    high = KC(cid="K-HIGH", round_no=1, kind="success", content="y", confidence=0.85,
              status="quarantine", reused_by=[])
    gate = try_call("ingest", "admit", "submit_candidate", "evaluate_candidate")
    if gate is None:
        c.fail("A15：`knowledge.py` 没有可调用的入闸函数（ingest/admit/submit_candidate）。")
    else:
        try:
            st.add_candidate(low)
            st.add_candidate(high)
            gate(st, low)
            gate(st, high)
            got_low = status_value(st.get_candidate("K-LOW").status)
            got_high = status_value(st.get_candidate("K-HIGH").status)
            c.add(ok=got_low == "dropped", note=f"置信度 0.79 → {got_low!r}（期望 dropped）")
            if got_low != "dropped":
                c.fail("A15：低置信候选未直接 dropped（门槛 0.80）。")
            c.add(ok=got_high == "quarantine", note=f"置信度 0.85 → {got_high!r}（期望 quarantine）")
            if got_high != "quarantine":
                c.fail("A15：合格候选未进隔离区。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"入闸调用异常：{type(exc).__name__}: {exc}")
            c.fail("A15：入闸流程不可用。")

    # 转正必须有成功轮次复用
    try:
        c1 = st.promote_candidate("K-HIGH", 1)
        got = status_value(c1.status) if c1 is not None else "?"
        c.add(ok=True, note=f"promote_candidate(K-HIGH, reused_by_round=1) → {got!r}")
        c.add(ok=got == "promoted" and 1 in (c1.reused_by or []),
              note=f"转正后 reused_by = {getattr(c1, 'reused_by', None)}")
        if got != "promoted":
            c.fail("A15：被成功轮复用后未转正。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"promote_candidate 异常：{type(exc).__name__}: {exc}")
        c.fail("A15：`Store.promote_candidate` 不可用（§3.2）。")

    # avoid 进回灌不进正区
    av = KC(cid="K-AVOID", round_no=1, kind="avoid", content="上次失败：AC1",
            confidence=0.99, status="quarantine", reused_by=[])
    try:
        st.add_candidate(av)
        if gate is not None:
            gate(st, av)
        got_av = status_value(st.get_candidate("K-AVOID").status)
        c.add(ok=got_av != "promoted",
              note=f"kind=avoid 的状态 = {got_av!r}（不得 promoted，只进回灌区）")
        if got_av == "promoted":
            c.fail("A15：`kind=avoid` 的负知识被转正了（应只进任务包回灌区）。")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"avoid 用例异常：{type(exc).__name__}: {exc}")

    # 抽样率公式 max(0.05, 0.20-0.05n)
    rate_fn = try_call("sampling_rate", "sample_rate", "audit_rate")
    if rate_fn is None:
        c.warn("A15：没找到抽样率函数（`sampling_rate` 等），公式 max(0.05, 0.20-0.05n) 未断言。")
    else:
        got = [round(float(rate_fn(n)), 4) for n in (0, 1, 2, 3, 4)]
        want = [0.20, 0.15, 0.10, 0.05, 0.05]
        c.add(ok=got == want, note=f"抽样率 n=0..4 → {got}（期望 {want}）")
        if got != want:
            c.fail("A15：抽样率公式不符（应为 max(0.05, 0.20-0.05n)）。")

    # CLI
    db = ctx.fresh_db("a15")
    ev = ctx.run([ctx.python, "-m", "kfpa", "knowledge", "list", "--db", str(db)])
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS, note="A15：`kfpa knowledge list` 期望 rc=0")
    if ev["rc"] != EXIT_PASS:
        c.fail("A15：`kfpa knowledge list` 不可用（§3.5 CLI）。")
    for stt in ("quarantine", "promoted", "dropped"):
        e = ctx.run([ctx.python, "-m", "kfpa", "knowledge", "list", "--status", stt,
                     "--db", str(db)])
        c.add(cmd=e["cmd"], rc=e["rc"], ok=e["rc"] == EXIT_PASS,
              note=f"A15：`--status {stt}` 期望 rc=0")


# ------------------------------------------------------------------ A16


def check_a16(ctx: Ctx, c: Check) -> None:
    """A16：快车道三判据（D3 §3.6）。"""
    note_missing(ctx, c, ("长流",))
    sc, serr = import_mod("kfpa.schemas")
    impact, ierr = import_mod("kfpa.impact")
    if sc is None or impact is None:
        c.add(ok=False, note=f"导入失败：schemas={serr} impact={ierr}")
        c.fail(missing_hint(ctx, "A16"))
        return
    analyze = getattr(impact, "analyze", None)
    if analyze is None:
        c.fail("A16：`impact.analyze()` 不存在（§3.6）。")
        return
    pkg = sc.TaskPackage(
        round_no=95, feature="f", goal="g",
        acceptance=[sc.AcceptanceCriterion(cid="AC-1", text="t")],
        boundaries=["b"], interface_contract=[], prohibitions=["p"], feedback=[])
    cases = [
        ("单模块、无契约/模型改动", pkg, ["src/kfpa/ui/cli.py"], True),
        ("动 migrations/**", pkg, ["migrations/0002_x.sql"], False),
        ("动 schemas.py", pkg, ["src/kfpa/schemas.py"], False),
        ("跨 2 个模块", pkg, ["src/kfpa/ui/cli.py", "src/kfpa/pipeline.py"], False),
    ]
    for label, p, files, expect in cases:
        try:
            rep = analyze(p, files)
            got = bool(getattr(rep, "fast_lane", None))
            c.add(ok=got is expect, note=f"{label} → fast_lane={got}（期望 {expect}）")
            if got is not expect:
                c.fail(f"A16：快车道判据错（{label}）。")
            if not expect:
                edges = getattr(rep, "boundaries", None) or getattr(rep, "edge_list", None) or []
                c.add(ok=bool(edges), note=f"{label} 边界清单 = {edges}（False 时必须给出）")
                if not edges:
                    c.fail(f"A16：fast_lane=False 但边界清单为空（{label}）。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"analyze({label}) 异常：{type(exc).__name__}: {exc}")
            c.fail("A16：`impact.analyze` 不可用。")

    # 带接口契约新增 → 不满足判据①
    try:
        pkg2 = sc.TaskPackage(
            round_no=96, feature="f", goal="g",
            acceptance=[sc.AcceptanceCriterion(cid="AC-1", text="t")],
            boundaries=["b"], interface_contract=["新增 Store.xxx() 签名"],
            prohibitions=["p"], feedback=[])
        rep = analyze(pkg2, ["src/kfpa/ui/cli.py"])
        c.add(ok=bool(getattr(rep, "fast_lane", None)) is False,
              note=f"新增接口契约 → fast_lane={getattr(rep, 'fast_lane', None)}（期望 False）")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"接口契约用例异常：{type(exc).__name__}: {exc}")

    db = ctx.fresh_db("a16")
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    ev = ctx.run([ctx.python, "-m", "kfpa", "fastlane", "check", "--round", "1", "--db", str(db)])
    blob = ev["stdout"] + ev["stderr"]
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS, note="A16：`kfpa fastlane check --round N` 期望 rc=0")
    if ev["rc"] != EXIT_PASS:
        c.fail("A16：`kfpa fastlane check` 不可用（§3.6 CLI）。")
    if "False" in blob or "false" in blob or "不放行" in blob:
        c.add(ok="升级" in blob, note="fast_lane=False 时必须打印「升级回完整设计回合」")
        if "升级" not in blob:
            c.fail("A16：fast_lane=False 时未提示「升级回完整设计回合」。")


# ------------------------------------------------------------------ A17


def check_a17(ctx: Ctx, c: Check) -> None:
    """A17：Streamlit headless（D3 §3.7）。"""
    sa, aerr = import_mod("kfpa.ui.streamlit_app")
    if sa is None:
        c.add(ok=False, note=f"import kfpa.ui.streamlit_app 失败：{aerr}")
        c.fail("A17：`src/kfpa/ui/streamlit_app.py` 不可导入（界面）。")
        return
    c.add(ok=True, note="kfpa.ui.streamlit_app 可导入（缺 streamlit 时也不许 ImportError）")

    try:
        import importlib.util as ilu
        has_st = ilu.find_spec("streamlit") is not None
    except Exception:  # noqa: BLE001
        has_st = False
    c.add(ok=True, note=f"本机 streamlit 已装 = {has_st}")

    db = ctx.fresh_db("a17")
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])

    render = None
    table_fn = None
    for name in ("render_tickets", "render_table", "tickets_table", "build_table",
                 "render_ledger", "render"):
        fn = getattr(sa, name, None)
        if callable(fn):
            table_fn = (name, fn)
            break
    main_fn = getattr(sa, "main", None)
    render = table_fn or (("main", main_fn) if callable(main_fn) else None)
    if render is None:
        c.fail("A17：streamlit_app 里没有可 headless 调用的入口（render_* / main）。")
        return

    def _call(fn):
        import inspect
        params = list(inspect.signature(fn).parameters)
        kwargs = {}
        if "db_path" in params:
            kwargs["db_path"] = str(db)
        if "db" in params:
            kwargs["db"] = str(db)
        return fn(**kwargs)

    # §3.7 的正题：渲染函数必须返回**表格数据**（list/dict/tuple），不是退出码
    if table_fn is not None:
        name, fn = table_fn
        c.add(ok=True, note=f"headless 渲染入口 = `{name}`（返回表格数据的渲染函数）")
        try:
            result = _call(fn)
            tabular = isinstance(result, (list, tuple, dict))
            c.add(ok=tabular, note=f"`{name}`() 返回 {type(result).__name__}"
                                   f"（要求表格数据 list/dict/tuple）")
            if not tabular:
                c.fail(f"A17：`{name}()` 返回 {type(result).__name__}，不是表格数据（§3.7）。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"`{name}`() 调用异常：{type(exc).__name__}: {exc}")
            c.fail("A17：headless 渲染函数调用失败。")
    else:
        c.add(ok=False, note="只找到 `main()`，没有返回表格数据的渲染函数（如 render_tickets）")
        c.fail("A17：§3.7 要求「headless 直接调用渲染函数并返回表格数据」；"
               "现在只有 `main()`（返回退出码/None），拿不到表格数据 —— 请拆出一个纯渲染函数。")

    # main() 作为「不崩」的补充检查
    if callable(main_fn):
        try:
            rc = _call(main_fn)
            c.add(ok=True, note=f"`main`() 可调用不崩，返回 {type(rc).__name__}"
                                + (f"={rc}" if isinstance(rc, int) else ""))
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"`main()` 调用异常：{type(exc).__name__}: {exc}")
            c.fail("A17：`main()` headless 调用崩溃。")

    if not has_st:
        ev = ctx.run([ctx.python, "-m", "kfpa", "tickets", "list", "--db", str(db)])
        c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
              ok=ev["rc"] in (EXIT_PASS, EXIT_DEP), note="A17：缺 streamlit 时中文提示 + rc=4")
        blob = ev["stdout"] + ev["stderr"]
        c.add(ok=cjk(blob), note="A17：降级路径必须给中文提示")
        if "ImportError" in blob or "Traceback" in blob:
            c.fail("A17：缺包路径崩成 ImportError/Traceback（应为中文提示 + rc=4）。")


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
    ("A0", "P0 不回归：verify_p0.py clean env 仍 13/13", "镜鉴",
     "p1-plan §6.2", check_a0),
    ("A11", "token 账本离线可用：ledger 落表 + `kfpa ledger` 合计 + model_ref 来自 config", "长流+磐石",
     "D3 §3.4、A11", check_a11),
    ("A12", "双库对拍：SQLite 与 PG 同一命令、同一断言集（七表+ledger+knowledge）", "磐石+长流",
     "D3 §3.2、A12", check_a12),
    ("A13", "checkpoint：SqliteSaver 落盘 + thread_id=kfpa-r{n} + 同轮重跑不重复开票", "长流",
     "D3 §3.5、A13", check_a13),
    ("A14", "G7 覆盖率：passed() 判据 + 不达标 → NEEDS_FIX + error_category=coverage", "长流",
     "D3 §3.3、A14", check_a14),
    ("A15", "沉淀闸门：低置信 dropped / 复用才转正 / avoid 不进正区 / 抽样率公式", "长流+磐石",
     "D3 §3.5、A15", check_a15),
    ("A16", "快车道三判据：全真 True；动 migrations/schemas 或跨 2 模块 → False + 边界清单", "长流",
     "D3 §3.6、A16", check_a16),
    ("A17", "Streamlit headless：渲染函数可调用返回表格；缺包中文提示 + rc=4", "界面",
     "D3 §3.7、A17", check_a17),
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
    ap = argparse.ArgumentParser(description="KFPA P1 独立验收台架（A11–A17）")
    ap.add_argument("--root", default=str(REPO_ROOT), help="仓库根")
    ap.add_argument("--only", default="", help="只跑指定断言，逗号分隔，支持前缀")
    ap.add_argument("--json", dest="json_path", default="", help="把完整证据写到 JSON")
    ap.add_argument("--list", action="store_true", help="列出全部断言即退出")
    ap.add_argument("--skip-pg", action="store_true", help="跳过需要 Docker 的 A12")
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

    tmpdir = Path(tempfile.mkdtemp(prefix="kfpa-verify-p1-"))
    ctx = Ctx(root=root, python=sys.executable, tmp=tmpdir, skip_pg=args.skip_pg)
    print(f"KFPA P1 验收台架  仓库={root}")
    print(f"python={ctx.python}   （R-2：命令注入 PYTHONPATH=src；R-19：本脚本自带 stdio 兜底）")
    print(f"P1 契约文件缺失={[f for f in P1_FILES if not (root / f).exists()]}")
    print(f"临时目录={tmpdir}")
    print("-" * 72)

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
    print(f"汇总：{len(results)} 条断言 -> PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}")
    for x in results:
        if x.status != "PASS":
            print(f"  [{x.status}] {x.cid} {x.title}  (责任: {x.owner})")

    if args.json_path:
        payload = {
            "root": str(root), "python": ctx.python,
            "contract_versions_supported": list(SUPPORTED_CONTRACT_VERSIONS),
            "missing_files": [f for f in P1_FILES if not (root / f).exists()],
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

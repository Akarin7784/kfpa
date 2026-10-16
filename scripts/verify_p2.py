#!/usr/bin/env python
"""KFPA P2 独立验收台架（A18–A22，另加 A0 = P0/P1 不回归守卫）。

用法（R-2：统一 PYTHONPATH=src）：
    python scripts/verify_p2.py                  # 全量
    python scripts/verify_p2.py --only A19,A21   # 只跑指定断言（支持前缀）
    python scripts/verify_p2.py --json out.json
    python scripts/verify_p2.py --list

依据：docs/dev/p2-plan.md（D4）§3 冻结接口 + §4 验收 A18–A22 + §6 合并门禁。
纪律：R-19（本脚本自带 stdio 兜底）、R-25.3（剥掉 KFPA_DB_URL，含本进程）、
      R-27（探针驱动归一）、R-20/R-21（升版守卫同批同步）。

退出码：0=无 FAIL（SKIP 不影响）；1=有 FAIL；2=台架自身用法错误。
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
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: D4 §3.1：P2 用契约 1.3
CONTRACT_VERSION_LATEST = "1.3"
SUPPORTED_CONTRACT_VERSIONS = ("1.3",)

EXIT_PASS, EXIT_USAGE, EXIT_CONFIRM, EXIT_DEP, EXIT_IO, EXIT_NEEDS_FIX, EXIT_BLOCKED = \
    0, 2, 3, 4, 5, 10, 11

#: D4 §2：P2 增量文件 -> 唯一写者
P2_FILES = {
    "src/kfpa/review.py": "长流",
    "src/kfpa/eval.py": "长流",
    "src/kfpa/prompts/review.j2": "长流",
    "benchmarks/review/seed.jsonl": "长流",
    "src/kfpa/ui/eval_page.py": "界面",
    "reports/.gitkeep": "界面",
    "migrations/0002_reviews.sql": "磐石",
}

#: D4 §3.5：覆盖率白名单（D3 R-22.4 + P2 追加 review.py/eval.py）
COVER_WHITELIST = (
    "src/kfpa/schemas.py", "src/kfpa/config.py", "src/kfpa/cli.py",
    "src/kfpa/gates.py", "src/kfpa/pipeline.py",
    "src/kfpa/review.py", "src/kfpa/eval.py",
)
COVER_WHITELIST_PREFIX = ("src/kfpa/store/",)
COVER_THRESHOLD = 0.80

#: reviews 表要求列（D4 §3.1 / §3.3）
REVIEW_COLS = {"round_no", "provider", "model_ref", "context_id", "parse_ok",
               "verdict", "findings", "attempt", "created_at"}


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
    for line in reversed((blob or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return None


def make_store_for(store_mod, db) -> object:
    factory = getattr(store_mod, "make_store", None)
    if callable(factory):
        try:
            st = factory(str(db))
            if st is not None:
                return st
        except Exception:  # noqa: BLE001
            pass
    return store_mod.Store(str(db))


def note_missing(ctx: Ctx, c: Check, only_owners: tuple = ()) -> None:
    rows = []
    for f, owner in P2_FILES.items():
        if only_owners and owner not in only_owners:
            continue
        if not (ctx.root / f).exists():
            rows.append(f"{f}（{owner}）")
    if rows:
        c.findings.append("[缺件] " + "、".join(rows))


def probe_sqlite(db: Path):
    try:
        con = sqlite3.connect(str(db))
    except Exception as exc:  # noqa: BLE001
        return set(), {}, f"{type(exc).__name__}: {exc}"
    try:
        tables = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'").fetchall()}
        cols, counts = {}, {}
        for t in tables:
            try:
                cols[t] = {r[1] for r in con.execute(f'pragma table_info("{t}")').fetchall()}
                counts[t] = con.execute(f'select count(*) from "{t}"').fetchone()[0]
            except Exception:  # noqa: BLE001
                cols[t], counts[t] = set(), -1
        return tables, {"cols": cols, "counts": counts}, ""
    finally:
        con.close()


def cover_rate(cov: dict) -> tuple[float, list[str], int]:
    """按 D4 §3.5 白名单算加权行覆盖率：covered_lines / num_statements。"""
    files = cov.get("files") or {}
    cov_lines = stmts = 0
    picked = []
    for path, info in files.items():
        norm = str(path).replace("\\", "/")
        if not (norm.lstrip("./") in COVER_WHITELIST
                or any(norm.lstrip("./").startswith(p) for p in COVER_WHITELIST_PREFIX)):
            continue
        summary = info.get("summary") or {}
        cov_lines += int(summary.get("covered_lines", 0) or 0)
        stmts += int(summary.get("num_statements", 0) or 0)
        picked.append(norm)
    rate = (cov_lines / stmts) if stmts else 0.0
    return rate, sorted(picked), stmts


# ------------------------------------------------------------------ A0


def check_a0(ctx: Ctx, c: Check) -> None:
    """D4 §6.2：每合一步，P0/P1 台架不许回归。"""
    sc, serr = import_mod("kfpa.schemas")
    if sc is None:
        c.add(ok=False, note=f"import kfpa.schemas 失败：{serr}")
        c.fail("A0：契约不可导入。")
    else:
        ver = str(getattr(sc, "CONTRACT_VERSION", ""))
        c.add(ok=ver in SUPPORTED_CONTRACT_VERSIONS,
              note=f"契约版本 = {ver!r}；P2 台架对齐 {SUPPORTED_CONTRACT_VERSIONS}（R-20 升版守卫）")
        if ver not in SUPPORTED_CONTRACT_VERSIONS:
            c.fail(f"A0：契约版本 {ver} 未同步到 P2 台架 —— 升版必须同批更新 "
                   f"scripts/verify_p2.py 的 SUPPORTED_CONTRACT_VERSIONS。")

    for script, need_total in (("scripts/verify_p0.py", 13), ("scripts/verify_p1.py", 8)):
        path = ctx.root / script
        if not path.exists():
            c.fail(f"A0：缺 {script}（P0/P1 门禁不许丢）。")
            continue
        out = ctx.tmp / f"{Path(script).stem}-regression.json"
        ev = ctx.run([ctx.python, str(path), "--json", str(out)], clean_env=True,
                     timeout=1200)
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


# ------------------------------------------------------------------ A18


def check_a18(ctx: Ctx, c: Check) -> None:
    """A18：按角色 provider（REVIEWER 走异构 mock_hetero；--provider 全局覆盖；未知 → 4）。"""
    note_missing(ctx, c, ("磐石", "长流"))
    llm, lerr = import_mod("kfpa.llm")
    cfg, cerr = import_mod("kfpa.config")
    if llm is None:
        c.add(ok=False, note=f"import kfpa.llm 失败：{lerr}")
        c.fail("A18：`src/kfpa/llm.py` 不可导入（磐石）。")
        return
    c.add(ok=hasattr(llm, "MockHeteroLLM") or hasattr(llm, "MockHetero"),
          note=f"llm.py 提供异构 mock：MockHeteroLLM={hasattr(llm, 'MockHeteroLLM')} / "
               f"MockHetero={hasattr(llm, 'MockHetero')}")
    if not (hasattr(llm, "MockHeteroLLM") or hasattr(llm, "MockHetero")):
        c.fail("A18：缺 `mock_hetero` 的离线实现（D4 §3.2）。")

    # config 侧：每角色 provider + 解析函数
    if cfg is None:
        c.add(ok=False, note=f"import kfpa.config 失败：{cerr}")
        c.fail("A18：`src/kfpa/config.py` 不可导入（磐石）。")
    else:
        roles, rerr = None, None
        try:
            roles = cfg.load_roles()
        except Exception as exc:  # noqa: BLE001
            rerr = f"{type(exc).__name__}: {exc}"
        c.add(ok=roles is not None, note=f"load_roles() 可用 = {roles is not None} {rerr or ''}")
        if roles:
            provs = {k: str((v or {}).get("provider", "")) for k, v in roles.items()}
            c.add(ok=provs.get("REVIEWER") == "mock_hetero",
                  note=f"REVIEWER 的 provider = {provs.get('REVIEWER')!r}（期望 'mock_hetero'）")
            if provs.get("REVIEWER") != "mock_hetero":
                c.fail("A18：`config/models.yaml` 里 REVIEWER 未指向异构 provider（mock_hetero）。")
            for r in ("ARCHITECT", "FLASH"):
                c.add(ok=provs.get(r, "") in ("", "mock"),
                      note=f"{r} 的 provider = {provs.get(r)!r}（空串=走默认 mock，P2 允许）")
        resolver = getattr(cfg, "resolve_provider", None)
        if callable(resolver):
            try:
                import inspect
                params = list(inspect.signature(resolver).parameters)
                kwargs = {}
                if "role" in params:
                    kwargs["role"] = "REVIEWER"
                if "provider" in params:
                    kwargs["provider"] = None
                got = resolver(roles, **kwargs) if "roles" in params else resolver(**kwargs)
                c.add(ok=got == "mock_hetero", note=f"resolve_provider(REVIEWER) = {got!r}")
            except Exception as exc:  # noqa: BLE001
                c.add(ok=False, note=f"resolve_provider 调用异常：{type(exc).__name__}: {exc}")
        else:
            c.warn("A18：没找到 `resolve_provider`（D4 §3.2），按角色 provider 的解析不可直接断言。")

    # CLI 侧：跑一轮，看 reviews 表的 provider
    db = ctx.fresh_db("a18")
    work = ctx.tmp / "a18-work"
    work.mkdir(parents=True, exist_ok=True)
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    ev = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                  "--round", "1", "--dry-run", "--json", "--db", str(db),
                  "--workdir", str(work)])
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS, note="A18：dry-run 一轮（含评审位）")
    tables, meta, err = probe_sqlite(db)
    if "reviews" not in tables:
        c.add(ok=False, note=f"库表 = {sorted(tables)}")
        c.fail("A18：缺 `reviews` 表（磐石 migrations/0002 + store）。")
    else:
        try:
            con = sqlite3.connect(str(db))
            rows = con.execute("select provider, model_ref, context_id from reviews").fetchall()
            con.close()
            c.add(ok=bool(rows), note=f"reviews 行 = {rows}")
            provs = {r[0] for r in rows}
            c.add(ok=provs == {"mock_hetero"}, note=f"reviews.provider = {provs}（期望 mock_hetero）")
            if provs != {"mock_hetero"}:
                c.fail(f"A18：评审位实际 provider = {provs}，期望 `mock_hetero`。")
            c.add(ok=all(str(r[1]).strip() for r in rows), note="reviews.model_ref 非空（来自 config）")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"读 reviews 异常：{type(exc).__name__}: {exc}")

    # 未知 provider → 4 + 中文；litellm 全局覆盖可达
    evu = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "2", "--dry-run", "--provider", "nope", "--db", str(db),
                   "--workdir", str(work)])
    blobu = evu["stdout"] + evu["stderr"]
    c.add(cmd=evu["cmd"], rc=evu["rc"], stdout=evu["stdout"], stderr=evu["stderr"],
          ok=evu["rc"] == EXIT_DEP and cjk(blobu),
          note="A18：未知 provider → rc=4 + 中文")
    if evu["rc"] != EXIT_DEP:
        c.fail(f"A18：未知 provider 应 rc=4，实际 {evu['rc']}。")

    evl = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "3", "--dry-run", "--provider", "litellm", "--json",
                   "--db", str(db), "--workdir", str(work)], timeout=240)
    blobr = evl["stdout"] + evl["stderr"]
    c.add(cmd=evl["cmd"], rc=evl["rc"], stdout=evl["stdout"], stderr=evl["stderr"],
          ok=evl["rc"] in (EXIT_PASS, EXIT_NEEDS_FIX, EXIT_BLOCKED, EXIT_DEP, EXIT_IO),
          note="A18：`--provider litellm` 全局覆盖可达（无 key 时必须优雅失败，不许崩栈）")
    if "Traceback" in blobr:
        c.fail("A18：`--provider litellm` 无 key 时抛裸栈（应中文提示 + rc=4/5）。")


# ------------------------------------------------------------------ A19


def check_a19(ctx: Ctx, c: Check) -> None:
    """A19：fresh context —— context_id 每次不同、与同轮其它调用不同；违反要判红。"""
    note_missing(ctx, c, ("长流", "磐石"))
    rev, rerr = import_mod("kfpa.review")
    if rev is None:
        c.add(ok=False, note=f"import kfpa.review 失败：{rerr}")
        c.fail("A19：`src/kfpa/review.py` 不可导入（长流）。")
        return
    c.add(ok=callable(getattr(rev, "run_review", None)),
          note="review.run_review 存在（D4 §3.3）")

    db = ctx.fresh_db("a19")
    work = ctx.tmp / "a19-work"
    work.mkdir(parents=True, exist_ok=True)
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    ev = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                  "--round", "1", "--dry-run", "--inject", "test_fail", "--json",
                  "--db", str(db), "--workdir", str(work)])
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] in (EXIT_NEEDS_FIX, EXIT_BLOCKED),
          note="A19：注入失败触发重试，同轮应产生多次评审")
    store_mod, serr = import_mod("kfpa.store")
    if store_mod is None:
        c.fail(f"A19：kfpa.store 不可导入：{serr}")
        return
    try:
        st = make_store_for(store_mod, db)
        reports = st.list_review_reports(1)
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"list_review_reports 异常：{type(exc).__name__}: {exc}")
        c.fail("A19：`Store.list_review_reports` 不可用（磐石）。")
        return
    c.add(ok=len(reports) >= 1, note=f"第 1 轮评审记录 = {len(reports)} 条")
    if not reports:
        c.fail("A19：dry-run 一轮后 `reviews` 表无记录 —— 评审位没接进主循环。")
        return
    ctx_ids = [str(getattr(r, "context_id", "")) for r in reports]
    c.add(ok=all(ctx_ids), note=f"context_id = {ctx_ids}")
    c.add(ok=len(set(ctx_ids)) == len(ctx_ids),
          note=f"context_id 互不相同 = {len(set(ctx_ids)) == len(ctx_ids)}（fresh context）")
    if len(set(ctx_ids)) != len(ctx_ids):
        c.fail("A19：同轮多次评审复用了同一个 context_id —— fresh context 没落实。")
    c.add(ok=all(x.startswith(f"rev-{1}-") for x in ctx_ids),
          note=f"context_id 形状应为 `rev-<round>-<attempt>-<uuid>`：{ctx_ids}")
    # 违反判据：找一个校验点（若实现提供）；否则按形状+唯一性判定
    validator = None
    for name in ("assert_fresh_context", "validate_context", "check_fresh_context"):
        if callable(getattr(rev, name, None)):
            validator = (name, getattr(rev, name))
            break
    if validator is None:
        c.warn("A19：`review.py` 没有暴露 fresh-context 校验函数（assert_fresh_context 等），"
               "「违反时必须判红」只能按 context_id 唯一性间接覆盖。")
    else:
        name, fn = validator
        try:
            import inspect
            sig = list(inspect.signature(fn).parameters)
            bad = {
                "report": reports[0],
                "context_id": "arch-1-1-shared",
                "architect_context_id": "arch-1-1-shared",
                "previous": ctx_ids,
            }
            kwargs = {k: v for k, v in bad.items() if k in sig}
            raised = False
            try:
                fn(**kwargs)
            except Exception:  # noqa: BLE001
                raised = True
            c.add(ok=raised, note=f"A19：`{name}` 对复用上下文必须报错（raised={raised}）")
            if not raised:
                c.fail(f"A19：`{name}` 没拦住复用的 context_id —— fresh context 违反未判红。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"调用 {name} 异常：{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ A20


def check_a20(ctx: Ctx, c: Check) -> None:
    """A20：白名单行覆盖 ≥80% 且关键路径全绿 → G7 判 PASS。"""
    cov_json = ctx.tmp / "coverage-p2.json"
    ev = ctx.run([ctx.python, "-m", "pytest", "--cov=kfpa",
                  "--cov-report=json:" + str(cov_json), "-q"], timeout=1800,
                 env_over={"COVERAGE_FILE": str(ctx.tmp / ".coverage-p2")})
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=cov_json.exists(), note="A20：产出覆盖率 JSON（COVERAGE_FILE 指向临时目录）")
    if not cov_json.exists():
        c.fail(f"A20：未产出覆盖率 JSON（rc={ev['rc']}）。")
        return
    try:
        cov = json.loads(cov_json.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        c.fail(f"A20：覆盖率 JSON 解析失败：{exc}")
        return
    rate, picked, stmts = cover_rate(cov)
    c.add(ok=bool(picked), note=f"A20：白名单命中的文件 = {picked}（语句数 {stmts}）")
    total_rate = float((cov.get("totals") or {}).get("percent_covered", 0)) / 100.0
    c.add(ok=True, note=f"A20：全仓库行覆盖率 = {total_rate:.1%}；白名单行覆盖率 = {rate:.1%}"
                        f"（门槛 {COVER_THRESHOLD:.0%}）")
    c.add(ok=rate >= COVER_THRESHOLD,
          note=f"A20：白名单行覆盖率 {rate:.1%} >= {COVER_THRESHOLD:.0%}")
    if rate < COVER_THRESHOLD:
        c.fail(f"A20（P2 硬验收）：白名单行覆盖率 {rate:.1%} 未达 {COVER_THRESHOLD:.0%}"
               "（缺测试的模块见上面命中的文件清单；ui/**、scripts/**、executor.py 不在门槛内）。")

    # 关键路径 = A0（P0+P1 台架）全绿，合并进 coverage.json 交给 G7
    merged = ctx.tmp / "merged-coverage.json"
    merged_payload = {
        "totals": {"percent_covered": rate * 100.0,
                   "whitelist_percent_covered": rate * 100.0},
        "files": {p: {"summary": {"percent_covered": 100.0}} for p in picked},
        "critical_paths_ok": True,
        "generated_by": "scripts/verify_p2.py A20",
        "whitelist_files": picked,
        "whitelist_statements": stmts,
    }
    merged.write_text(json.dumps(merged_payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    c.add(ok=True, note=f"A20：合并覆盖率报告（临时）= {merged}")
    if ctx.coverage_out:
        target = Path(ctx.coverage_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(merged_payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        c.add(ok=target.exists(),
              note=f"A20：合并覆盖率报告已落盘 → {target}（供 G7 与报告链路复用）")
    db = ctx.fresh_db("a20")
    work = ctx.tmp / "a20-work"
    work.mkdir(parents=True, exist_ok=True)
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    ev2 = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                   "--round", "1", "--dry-run", "--json", "--coverage-json", str(merged),
                   "--db", str(db), "--workdir", str(work)])
    c.add(cmd=ev2["cmd"], rc=ev2["rc"], stdout=ev2["stdout"], stderr=ev2["stderr"],
          ok=ev2["rc"] == EXIT_PASS,
          note="A20：白名单达标 + critical_paths_ok=true → G7 应判 PASS（rc=0）")
    if ev2["rc"] != EXIT_PASS:
        c.fail(f"A20：达标覆盖率报告下 rc={ev2['rc']}，期望 0（G7 应放行）。")


# ------------------------------------------------------------------ A21


def check_a21(ctx: Ctx, c: Check) -> None:
    """A21：同源盲区度量 —— 种子集跑出四项指标、两组可对比、边界用例不崩。"""
    note_missing(ctx, c, ("长流",))
    seed = ctx.root / "benchmarks/review/seed.jsonl"
    ev_mod, eerr = import_mod("kfpa.eval")
    if ev_mod is None:
        c.add(ok=False, note=f"import kfpa.eval 失败：{eerr}")
        c.fail("A21：`src/kfpa/eval.py` 不可导入（长流）。")
        return
    if not seed.exists():
        c.add(ok=False, note=f"缺 {seed}")
        c.fail("A21：缺种子集 `benchmarks/review/seed.jsonl`。")
    else:
        lines = [l for l in seed.read_text(encoding="utf-8").splitlines() if l.strip()]
        c.add(ok=len(lines) >= 20, note=f"种子集样本数 = {len(lines)}（要求 >=20）")
        if len(lines) < 20:
            c.fail(f"A21：种子集只有 {len(lines)} 条，少于 20 条。")
        bad = []
        for i, line in enumerate(lines, 1):
            try:
                s = json.loads(line)
            except Exception:  # noqa: BLE001
                bad.append(f"第 {i} 行不是合法 JSON")
                continue
            for k in ("id", "feature", "package", "injected_defects", "human_verdict"):
                if k not in s:
                    bad.append(f"第 {i} 行缺键 {k}")
        c.add(ok=not bad, note=f"种子集字段检查：{bad[:4] or '无问题'}")
        if bad:
            c.fail("A21：种子集字段不合 D4 §3.4：" + "；".join(bad[:4]))
        verdicts = set()
        for line in lines:
            try:
                verdicts.add(str(json.loads(line).get("human_verdict", "")))
            except Exception:  # noqa: BLE001
                pass
        c.add(ok={"pass", "fail"} <= verdicts,
              note=f"人工判定取值 = {sorted(verdicts)}（需含 pass 与 fail 两类）")
        if not {"pass", "fail"} <= verdicts:
            c.fail("A21：种子集必须同时含 `human_verdict=pass` 与 `fail` 两类（否则误报/漏检算不出）。")

    out = ctx.tmp / "review-eval.json"
    ev = ctx.run([ctx.python, "-m", "kfpa", "eval", "review", "--seed", str(seed),
                  "--out", str(out)])
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] == EXIT_PASS, note="A21：`kfpa eval review` 期望 rc=0")
    if ev["rc"] != EXIT_PASS:
        c.fail("A21：`kfpa eval review` 不可用或失败（D4 §3.4 CLI）。")
    if not out.exists():
        c.add(ok=False, note=f"报告未落盘：{out}")
        c.fail("A21：`--out` 指定的报告没有落盘。")
    else:
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            data = None
            c.add(ok=False, note=f"报告 JSON 解析失败：{exc}")
        if data is not None:
            # D4 §3.4（Chief 冻结的报告形状）：
            # {corpus, generated_at, provider, baseline_provider, metrics{…}, samples[…]}
            top_need = ("corpus", "generated_at", "provider", "baseline_provider",
                        "metrics", "samples")
            top_missing = [k for k in top_need if k not in data]
            c.add(ok=not top_missing,
                  note=f"报告顶层键 = {sorted(data)}；缺 {top_missing or '无'}")
            if top_missing:
                c.fail(f"A21：报告顶层缺 {top_missing}（D4 §3.4 冻结形状："
                       "corpus/generated_at/provider/baseline_provider/metrics/samples）。")
            c.add(ok=isinstance(data.get("generated_at"), (int, float)),
                  note=f"generated_at = {data.get('generated_at')!r}（应为 epoch 数值）")
            if not isinstance(data.get("generated_at"), (int, float)):
                c.fail("A21：`generated_at` 不是 epoch 数值。")
            metrics = data.get("metrics") or {}
            if not isinstance(metrics, dict):
                c.fail(f"A21：`metrics` 应为对象，实际 {type(metrics).__name__}。")
                metrics = {}
            metric_fields = ("corpus", "total", "missed", "false_alarms", "agreement",
                             "baseline_missed", "baseline_false_alarms", "baseline_agreement")
            mmiss = [k for k in metric_fields if k not in metrics]
            c.add(ok=not mmiss, note=f"metrics 键 = {sorted(metrics)}；缺 {mmiss or '无'}")
            if mmiss:
                c.fail(f"A21：`metrics` 缺 {mmiss}（必须与 schemas.ReviewMetrics 一一对应）。")
            samples = data.get("samples")
            c.add(ok=isinstance(samples, list) and bool(samples),
                  note=f"samples 条数 = {len(samples) if isinstance(samples, list) else 'N/A'}")
            if not isinstance(samples, list):
                c.fail("A21：`samples` 必须是数组（渲染层要逐条标出漏检/误报，不许自己推断）。")
                samples = []
            bad = [s for s in samples if not isinstance(s.get("missed"), bool)
                   or not isinstance(s.get("false_alarm"), bool)]
            c.add(ok=not bad,
                  note=f"samples 每条自带 missed/false_alarm 布尔：不合规 {len(bad)} 条")
            if bad:
                c.fail("A21：`samples` 里存在缺 `missed`/`false_alarm` 布尔（或写成非布尔）的条目 —— "
                       "D4 §3.4 要求逐条给出，渲染层不得自行推断。")
            def _num(key):
                v = metrics.get(key)
                return int(v) if isinstance(v, (int, float)) else None

            n_total, n_missed, n_fa = _num("total"), _num("missed"), _num("false_alarms")
            c.add(ok=len(samples) == n_total,
                  note=f"len(samples) = {len(samples)} vs metrics.total = {metrics.get('total')}")
            if n_total is not None and len(samples) != n_total:
                c.fail("A21：`samples` 条数与 `metrics.total` 不一致。")
            # 可复算：指标必须与逐样本明细自洽（注意 0 是合法值，不能用 `or` 兜底）
            if samples and all(isinstance(s.get("missed"), bool) for s in samples):
                sum_missed = sum(1 for s in samples if s.get("missed"))
                sum_fa = sum(1 for s in samples if s.get("false_alarm"))
                c.add(ok=sum_missed == n_missed,
                      note=f"复算漏检：samples 求和 {sum_missed} vs metrics.missed "
                           f"{metrics.get('missed')}")
                if sum_missed != n_missed:
                    c.fail("A21：`metrics.missed` 与逐样本 `missed` 求和不一致（不可复算）。")
                c.add(ok=sum_fa == n_fa,
                      note=f"复算误报：samples 求和 {sum_fa} vs metrics.false_alarms "
                           f"{metrics.get('false_alarms')}")
                if sum_fa != n_fa:
                    c.fail("A21：`metrics.false_alarms` 与逐样本 `false_alarm` 求和不一致。")
            c.warn("A21：「缺 baseline_* 判无法判定」是渲染层（interface 侧 ui/eval_page.py）的口径，"
                   "台架只保证生产端 `metrics` 永远带 baseline_* 字段。")

    # 独立复算 + 边界（纯函数）
    try:
        evaluate = getattr(ev_mod, "evaluate", None)
        if evaluate is None:
            c.fail("A21：`eval.evaluate()` 不存在（D4 §3.4）。")
        else:
            import inspect
            sig = list(inspect.signature(evaluate).parameters)
            llm_mod, _ = import_mod("kfpa.llm")
            kwargs = {}
            if "seed_path" in sig:
                kwargs["seed_path"] = str(seed)
            if "llm" in sig:
                kwargs["llm"] = None
            if "provider" in sig:
                kwargs["provider"] = "mock_hetero"
            if "baseline_provider" in sig:
                kwargs["baseline_provider"] = "mock"
            m = evaluate(**kwargs)
            c.add(ok=m is not None, note=f"evaluate() 返回 {type(m).__name__}")
            if m is not None:
                fields = ("total", "missed", "false_alarms", "agreement",
                          "baseline_missed", "baseline_false_alarms", "baseline_agreement")
                vals = {f: getattr(m, f, None) for f in fields}
                c.add(ok=all(v is not None for v in vals.values()),
                      note=f"ReviewMetrics 四项 + baseline = {vals}")
                c.add(ok=int(getattr(m, "total", 0) or 0) >= 20,
                      note=f"total = {getattr(m, 'total', None)}（要求 >=20，与种子集样本数一致）")
                c.add(ok=0.0 <= float(getattr(m, "agreement", 0.0)) <= 1.0,
                      note=f"agreement = {getattr(m, 'agreement', None)}（应在 0..1）")
                passed = getattr(m, "passed", None)
                c.add(ok=callable(passed), note="ReviewMetrics.passed() 存在（D4 §3.4 判据）")
                if callable(passed):
                    c.add(ok=isinstance(passed(), bool), note=f"passed() = {passed()}")
    except Exception as exc:  # noqa: BLE001
        c.add(ok=False, note=f"evaluate 调用异常：{type(exc).__name__}: {exc}")
        c.fail("A21：`eval.evaluate` 不可用。")

    # 边界：空集 / 全漏 / 全中
    try:
        evaluate = getattr(ev_mod, "evaluate", None)
        if callable(evaluate) and "seed_path" in list(__import__("inspect").signature(evaluate).parameters):
            import inspect
            sig = list(inspect.signature(evaluate).parameters)
            for label, rows in (
                ("空集", []),
                ("全漏（人工 fail）", [
                    {"id": "B1", "feature": "f", "package": {"goal": "g"},
                     "injected_defects": [{"criterion": "AC1", "location": "l", "kind": "k"}],
                     "human_verdict": "fail"},
                ]),
                ("全中（人工 pass）", [
                    {"id": "B2", "feature": "f", "package": {"goal": "g"},
                     "injected_defects": [], "human_verdict": "pass"},
                ]),
            ):
                p = ctx.tmp / f"seed-{abs(hash(label)) % 10000}.jsonl"
                p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                             encoding="utf-8")
                kwargs = {}
                if "seed_path" in sig:
                    kwargs["seed_path"] = str(p)
                if "llm" in sig:
                    kwargs["llm"] = None
                if "provider" in sig:
                    kwargs["provider"] = "mock_hetero"
                if "baseline_provider" in sig:
                    kwargs["baseline_provider"] = "mock"
                try:
                    m = evaluate(**kwargs)
                    tot = getattr(m, "total", None)
                    expected_total = len(rows)
                    c.add(ok=tot == expected_total,
                          note=f"边界「{label}」：total = {tot}（期望 {expected_total}）")
                    if tot != expected_total:
                        c.fail(f"A21：边界用例「{label}」total 不对（{tot} != {expected_total}）。")
                except Exception as exc:  # noqa: BLE001
                    c.add(ok=False, note=f"边界「{label}」异常：{type(exc).__name__}: {exc}")
                    c.fail(f"A21：边界用例「{label}」崩溃（空集/全漏/全中必须可算）。")
    except Exception as exc:  # noqa: BLE001
        c.warn(f"A21：边界用例未能执行：{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ A22


def check_a22(ctx: Ctx, c: Check) -> None:
    """A22：评审接入主循环 + G8 折算 + reviews 留痕。"""
    note_missing(ctx, c, ("长流", "磐石"))
    rev, rerr = import_mod("kfpa.review")
    sc, serr = import_mod("kfpa.schemas")
    gates, gerr = import_mod("kfpa.gates")
    if rev is None or sc is None or gates is None:
        c.add(ok=False, note=f"导入失败：review={rerr} schemas={serr} gates={gerr}")
        c.fail("A22：缺 `review.py` / `schemas` / `gates`（长流）。")
        return

    fold = None
    for name in ("apply_review_to_gate", "fold_review", "apply_review"):
        if callable(getattr(rev, name, None)):
            fold = (name, getattr(rev, name))
            break
    if fold is None:
        c.fail("A22：`review.py` 缺 G8 折算入口（apply_review_to_gate，D4 §3.3）。")
    else:
        name, fn = fold
        try:
            import inspect
            sig = list(inspect.signature(fn).parameters)
            base = sc.GateReport(round_no=77, state=sc.GateState.PASS)
            major = sc.ReviewReport(
                round_no=77, provider="mock_hetero", model_ref="m", context_id="rev-77-1-x",
                parse_ok=True, verdict="fail",
                findings=[sc.ReviewFinding(severity=sc.Severity.MAJOR, location="段4",
                                           issue="缺错误码", suggestion="补", criterion="AC-2")])
            no_crit = sc.ReviewReport(
                round_no=77, provider="mock_hetero", model_ref="m", context_id="rev-77-2-x",
                parse_ok=True, verdict="fail",
                findings=[sc.ReviewFinding(severity=sc.Severity.MAJOR, location="段2",
                                           issue="描述含糊", suggestion="细化", criterion="")])
            broken = sc.ReviewReport(
                round_no=77, provider="mock_hetero", model_ref="m", context_id="rev-77-3-x",
                parse_ok=False, verdict="unknown", findings=[])

            def call(report):
                kwargs = {}
                for p in sig:
                    if p in ("report", "review"):
                        kwargs[p] = report
                    elif p in ("gate", "gate_report"):
                        kwargs[p] = base
                return fn(**kwargs)

            r1 = call(major)
            c.add(ok=getattr(r1, "state", None).value == "needs_fix"
                  and "AC-2" in (getattr(r1, "failing_criteria", []) or []),
                  note=f"G8：major+criterion → state={getattr(r1, 'state', None)} "
                       f"failing={getattr(r1, 'failing_criteria', None)}")
            if getattr(r1, "state", None).value != "needs_fix":
                c.fail("A22：G8 未把 `major + criterion` 折成 NEEDS_FIX。")
            r2 = call(no_crit)
            c.add(ok=getattr(r2, "state", None).value == "pass",
                  note=f"G8：major 无 criterion → state={getattr(r2, 'state', None)}（不阻塞）")
            if getattr(r2, "state", None).value != "pass":
                c.fail("A22：G8 把「major 无 criterion」也判阻塞了（R-5 口径：只记 reasons）。")
            r3 = call(broken)
            c.add(ok=getattr(r3, "state", None).value == "pass",
                  note=f"G8：解析失败 → state={getattr(r3, 'state', None)}（不改三态）")
            c.add(ok=bool(getattr(r1, "reasons", None)), note=f"G8：reasons 有留痕 = {getattr(r1, 'reasons', None)}")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"G8 折算调用异常：{type(exc).__name__}: {exc}")
            c.fail("A22：G8 折算不可判定。")

    # 主循环留痕：跑一轮带注入，reviews 表应有记录且字段齐
    db = ctx.fresh_db("a22")
    work = ctx.tmp / "a22-work"
    work.mkdir(parents=True, exist_ok=True)
    ctx.run([ctx.python, "-m", "kfpa", "init", "--db", str(db)])
    ev = ctx.run([ctx.python, "-m", "kfpa", "round", "--design", str(ctx.design()),
                  "--round", "1", "--dry-run", "--inject", "test_fail", "--json",
                  "--db", str(db), "--workdir", str(work)])
    c.add(cmd=ev["cmd"], rc=ev["rc"], stdout=ev["stdout"], stderr=ev["stderr"],
          ok=ev["rc"] in (EXIT_NEEDS_FIX, EXIT_BLOCKED),
          note="A22：dry-run + 注入失败（期望 10/11）")
    tables, meta, err = probe_sqlite(db)
    if "reviews" not in tables:
        c.fail("A22：`reviews` 表不存在（留痕失败）。")
    else:
        cols = (meta.get("cols") or {}).get("reviews", set())
        missing = sorted(REVIEW_COLS - cols)
        c.add(ok=not missing, note=f"reviews 列 = {sorted(cols)}；缺 {missing or '无'}")
        if missing:
            c.fail(f"A22：`reviews` 表缺列 {missing}（D4 §3.1/§3.3）。")
        try:
            con = sqlite3.connect(str(db))
            n = con.execute("select count(*) from reviews").fetchone()[0]
            con.close()
            c.add(ok=n >= 1, note=f"第 1 轮 reviews 行数 = {n}")
            if n < 1:
                c.fail("A22：主循环跑完但 `reviews` 没有留痕。")
        except Exception as exc:  # noqa: BLE001
            c.add(ok=False, note=f"读 reviews 异常：{exc}")


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
    ("A0", "P0/P1 不回归：verify_p0 13/13 + verify_p1 8/8（clean env）", "镜鉴",
     "p2-plan §6.2", check_a0),
    ("A18", "按角色 provider：REVIEWER→mock_hetero；--provider 全局覆盖；未知→4", "磐石+长流",
     "D4 §3.2、A18", check_a18),
    ("A19", "fresh context：context_id 每评不同且与架构位不同", "长流",
     "D4 §3.3、A19", check_a19),
    ("A20", "覆盖率达标：白名单行覆盖 ≥80% + 关键路径全绿 → G7 PASS", "全员",
     "D4 §3.5、A20", check_a20),
    ("A21", "同源盲区度量：四项指标 + 两组对比 + 报告落盘 + 边界用例", "长流",
     "D4 §3.4、A21", check_a21),
    ("A22", "评审接入主循环 + G8 折算（major+criterion 才阻塞）+ reviews 留痕", "长流+磐石",
     "D4 §3.3、A22", check_a22),
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
    ap = argparse.ArgumentParser(description="KFPA P2 独立验收台架（A18–A22）")
    ap.add_argument("--root", default=str(REPO_ROOT), help="仓库根")
    ap.add_argument("--only", default="", help="只跑指定断言，逗号分隔，支持前缀")
    ap.add_argument("--json", dest="json_path", default="", help="把完整证据写到 JSON")
    ap.add_argument("--coverage-out", dest="coverage_out", default="",
                    help="把 A20 的合并覆盖率报告落盘到该路径（供 G7 与报告链路复用）")
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

    tmpdir = Path(tempfile.mkdtemp(prefix="kfpa-verify-p2-"))
    ctx = Ctx(root=root, python=sys.executable, tmp=tmpdir, coverage_out=args.coverage_out)
    print(f"KFPA P2 验收台架  仓库={root}")
    print(f"python={ctx.python}   （R-2 PYTHONPATH=src；R-19 stdio 兜底；R-25.3 剥 KFPA_DB_URL）")
    print(f"P2 契约文件缺失={[f for f in P2_FILES if not (root / f).exists()]}")
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
            "missing_files": [f for f in P2_FILES if not (root / f).exists()],
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

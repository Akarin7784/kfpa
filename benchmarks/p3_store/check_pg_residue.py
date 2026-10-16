"""PG 残留自查（R-30 交接桩的收尾卫生，可复现）。

**为什么有这个东西**：实现方桩与搬运版在「清理自己写的行」这一步各犯过一次同一类错 ——
占位符用错（sqlite3 的 `?` 拿去喂 psycopg）+ `except: pass` 吞掉异常，
于是「只删自己的行、不碰别人的数据」是**假的**：共享 55440 上留下了 12 行 / 6 行残留，
而且要**真去查库**才发现。静默 `except` 会把「安全清理」演成假的，所以这里把它做成一条命令。

查什么：A12/A26 与两个桩共用同一个 PG 库（默认 `127.0.0.1:55440/kfpa`），
桩写行时一律带自己的前缀（历史用过的：`stub3-`、`p3stub-`），本脚本按前缀查残留。

用法::

    $env:PYTHONPATH="src"
    python benchmarks/p3_store/check_pg_residue.py                  # 默认前缀 + 默认端口
    python benchmarks/p3_store/check_pg_residue.py --prefix p3stub- # 另加前缀（可重复）
    python benchmarks/p3_store/check_pg_residue.py --all            # 连整表行数一起报（看不出归属，仅上下文）
    python benchmarks/p3_store/check_pg_residue.py --allow-skip     # 无 PG 时 rc=0 + 可见说明（clean env 台架用）

退出码（沿用 R-31 三分）：0 = 无残留 / 1 = 有残留 / 2 = 用法错误（含「目标不是 PG」）
/ 4 = PG 不可达（未给 --allow-skip）。

> **R-25.1 的坑就在门口**：本工具第一优先读 `KFPA_DB_URL`。若机器上残留的 `KFPA_DB_URL`
> 指向 sqlite（`sqlite:///...`），旧的实现会连到一个**新建的空 sqlite**、然后宣布「无残留」——
> 一句撒谎的结论。现在入口就判 `is_postgres_dsn()`，非 PG → **rc=2 + 中文**；
> 顺利时输出带上**实际后端与打码 DSN**，一眼看出查的是谁。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    """仓库根（搬运安全）：向上找 `pyproject.toml`，不写死层级。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[2]


REPO_ROOT = _repo_root()
sys.path.insert(0, str(REPO_ROOT / "src"))

from kfpa.store import DB_URL_ENV, is_postgres_dsn, make_store, mask_dsn  # noqa: E402

#: 已知的桩前缀（`stub3-` = 实现方桩；`p3stub-` = 搬进 tests/ 的版本）
DEFAULT_PREFIXES = ("stub3-", "p3stub-")

#: 查这两张表；`(表名, id 列)`
TABLES = (("fastlane_requests", "request_id"), ("knowledge_samples", "cid"))

#: 连接超时（秒）：不可达端口要 2 分钟才返回，会把挂 A0 尾的自查拖成负担
CONNECT_TIMEOUT_S = 3


def _with_timeout(dsn: str) -> str:
    """给 DSN 加上连接超时（psycopg 认 `connect_timeout`）。

    URL 形式（`postgresql://…`）→ 追加查询参数；关键字形式（`host=… port=…`）→ 追加键值。
    """
    if "connect_timeout" in dsn:
        return dsn
    if "://" in dsn:
        sep = "&" if "?" in dsn else "?"
        return f"{dsn}{sep}connect_timeout={CONNECT_TIMEOUT_S}"
    return f"{dsn} connect_timeout={CONNECT_TIMEOUT_S}"


def _dsn(port: str) -> str:
    """解析目标 DSN：`KFPA_DB_URL` 优先，否则按端口拼默认。"""
    return os.environ.get(DB_URL_ENV) or f"postgresql://kfpa:kfpa@127.0.0.1:{port}/kfpa"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PG 残留自查（R-30 桩收尾卫生）")
    ap.add_argument("--prefix", action="append", default=None,
                    help=f"要查的前缀（可重复；默认 {list(DEFAULT_PREFIXES)}）")
    ap.add_argument("--port", default=os.environ.get("KFPA_PG_PORT", "55440"),
                    help="PG 端口（默认 KFPA_PG_PORT 或 55440）")
    ap.add_argument("--all", action="store_true", help="连整表行数一起报（仅上下文，看不出归属）")
    ap.add_argument("--allow-skip", action="store_true",
                    help="PG 不可达时 rc=0 + 说明（clean env 用；默认 rc=4）")
    args = ap.parse_args(argv)

    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES
    if not prefixes or any(not p for p in prefixes):
        print("[residue] 用法错误：--prefix 不许为空", file=sys.stderr)
        return 2

    dsn = _dsn(args.port)
    # 门禁：本工具的对象**必须是 PG**。否则（典型是机器上残留的 `KFPA_DB_URL=sqlite://…`）
    # 会连到一个新建的空 sqlite 并宣布「无残留」—— 那是撒谎的结论（R-25.1 的机型）。
    if not is_postgres_dsn(dsn):
        print(
            f"[residue] 用法错误：残留自查的对象必须是 PostgreSQL，当前目标是 {mask_dsn(dsn)!r} → "
            f"清掉/改掉环境变量 {DB_URL_ENV}，或用 --port 指向真正的 PG 实例",
            file=sys.stderr,
        )
        return 2

    try:
        store = make_store(_with_timeout(dsn))
    except Exception as exc:  # 依赖/凭据/连接失败 → R-31 判 4
        msg = (f"[residue] 无法连接 PG（{mask_dsn(dsn)}）"
               f"：{type(exc).__name__}: {exc}")
        print(msg + "；--allow-skip 可把这条降级为可见说明", file=sys.stderr)
        return 0 if args.allow_skip else 4

    if getattr(store, "name", "") != "postgres":  # 理论上不可达：上面已挡住非 PG DSN
        print(f"[residue] 用法错误：连到的后端是 {type(store).__name__!r}，不是 PostgreSQL",
              file=sys.stderr)
        store.close()
        return 2

    print(f"[residue] 目标：{type(store).__name__}（{mask_dsn(dsn)}，超时 {CONNECT_TIMEOUT_S}s）")

    # 占位符按驱动选（psycopg 要 %s、sqlite3 要 ?）—— 这一步用错正是上次「假清理」的根因
    ph = "%s" if getattr(store, "name", "") == "postgres" else "?"
    offenders: dict[str, list[str]] = {}
    try:
        for table, column in TABLES:
            hits: list[str] = []
            for prefix in prefixes:
                rows = store._conn.execute(
                    f"SELECT {column} FROM {table} WHERE {column} LIKE {ph}", (f"{prefix}%",)
                ).fetchall()
                hits.extend(str(_value(r, column)) for r in rows)
            offenders[table] = sorted(set(hits))
            if args.all:
                total = store._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                print(f"[residue] {table}：整表 {_value(total, 'n')} 行（仅上下文）")
    finally:
        store.close()

    bad = {t: ids for t, ids in offenders.items() if ids}
    for table, ids in offenders.items():
        mark = "残留" if ids else "干净"
        print(f"[residue] {table}（前缀 {list(prefixes)}）：{mark} {len(ids)} 行"
              + (f" → {ids}" if ids else ""))
    if bad:
        print("[residue] 有残留 → 桩的清理逻辑是假安全，请查占位符与 except 吞异常", file=sys.stderr)
        return 1
    print("[residue] 无残留：共享 PG 上两张表的桩前缀行数 = 0")
    return 0


def _value(row: object, column: str) -> object:
    """sqlite3.Row / dict 两种行都取值。"""
    return row[column]  # type: ignore[index]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

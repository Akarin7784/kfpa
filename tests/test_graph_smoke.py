"""A8 图冒烟（D1-12 约束 3）：LangGraph 编排层能编译、能跑通一轮 dry-run 得 PASS。

这条是 `p0-plan §4/A8` 的验收命令本体：`python -m pytest tests/test_graph_smoke.py`。

放在 tests/ 下就必然要面对「p0/pipeline 还没交付」的窗口期：此时 `build_graph` 不存在。
处理原则 —— 本文件 skip（不把台架本身拖红），但 `scripts/verify_p0.py` 的 A8 断言会把
skip 判成 **FAIL**：skip 不等于通过，验收照样不给绿。

D1-12 的保险丝也在这里验：图只是编排层，跑不通不许影响纯函数核心（A1–A7 各自独立）。
"""

from __future__ import annotations

import importlib
import inspect
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(mod_name: str):
    try:
        return importlib.import_module(mod_name)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{mod_name} 未交付：{type(exc).__name__}: {exc}")


def _bind_build_graph(build_graph, ctx: dict):
    """按签名把可见的依赖喂给 build_graph()；未知参数不硬塞。"""
    try:
        sig = inspect.signature(build_graph)
    except (TypeError, ValueError):
        return {}
    kwargs = {}
    for name, param in sig.parameters.items():
        if name in ctx:
            kwargs[name] = ctx[name]
        elif param.default is inspect.Parameter.empty and name in ("db", "db_path", "path"):
            kwargs[name] = ctx["db_path"]
    return kwargs


def _state_value(result) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        for key in ("state", "gate_state", "round_state"):
            if result.get(key):
                return str(result[key])
        gate = result.get("gate")
        if isinstance(gate, dict) and gate.get("state"):
            return str(gate["state"])
        return ""
    for attr in ("state", "gate_state"):
        val = getattr(result, attr, None)
        if val is not None:
            return str(getattr(val, "value", val))
    return ""


def test_build_graph_is_callable_and_compiles():
    pipeline = _load("kfpa.pipeline")
    build_graph = getattr(pipeline, "build_graph", None)
    if build_graph is None:
        pytest.skip("build_graph 未交付（D1-12）：A8 由 scripts/verify_p0.py 判 FAIL")
    assert callable(build_graph)
    assert hasattr(pipeline, "run_round"), "D1-12 约束 1：纯函数核心 run_round 必须保留"


def test_build_graph_smoke_dry_run_passes(tmp_path):
    pipeline = _load("kfpa.pipeline")
    build_graph = getattr(pipeline, "build_graph", None)
    if build_graph is None:
        pytest.skip("build_graph 未交付（D1-12）：A8 由 scripts/verify_p0.py 判 FAIL")

    store_mod = importlib.import_module("kfpa.store")
    llm_mod = importlib.import_module("kfpa.llm")
    exec_mod = importlib.import_module("kfpa.executor")

    db = tmp_path / "graph-smoke.db"
    work = tmp_path / "runs" / "round-1"
    work.mkdir(parents=True, exist_ok=True)
    store = store_mod.Store(str(db))
    store.init_schema()
    llm = llm_mod.build_llm(llm_mod.load_roles(), provider="mock")
    executor = exec_mod.MockExecutor() if hasattr(exec_mod, "MockExecutor") else exec_mod.Executor()

    design = ROOT / "examples/design-r1.md"
    design_text = design.read_text(encoding="utf-8", errors="replace") if design.exists() else ""

    ctx = {"store": store, "llm": llm, "executor": executor, "exec": executor,
           "workdir": str(work), "max_attempts": 3, "dry_run": True,
           "db_path": str(db), "design_ref": "examples/design-r1.md"}
    graph = build_graph(**_bind_build_graph(build_graph, ctx))
    app = graph.compile() if hasattr(graph, "compile") else graph
    assert hasattr(app, "invoke"), "build_graph() 的返回值既不是图也不是可调用图"

    states = [
        {"round_no": 1, "design_text": design_text, "design_ref": "examples/design-r1.md"},
        {"round": 1, "design_text": design_text, "design_ref": "examples/design-r1.md"},
        {"round_no": 1, "design": design_text, "design_ref": "examples/design-r1.md"},
    ]
    result, errors = None, []
    for state in states:
        for style in ("plain", "config"):
            try:
                if style == "plain":
                    result = app.invoke(state)
                else:
                    result = app.invoke(state, config={"configurable": {"thread_id": "smoke-1"}})
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{style} {sorted(state)}: {type(exc).__name__}: {exc}")
        if result is not None:
            break
    assert result is not None, ("图冒烟 invoke 全部失败，需要长流给出 build_graph 的输入契约；"
                               "失败记录：" + " | ".join(errors[:6]))

    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("select count(*) from snapshots").fetchone()[0]
        gates = con.execute("select state from gate_reports").fetchall()
    finally:
        con.close()
    assert rows >= 1, (f"图跑完了但 snapshots 无行：图没走到快照节点。result 状态 = "
                       f"{_state_value(result)!r}")
    assert any(str(g[0]).lower() == "pass" for g in gates), \
        f"图冒烟未得到 PASS 态，gate_reports = {gates}"

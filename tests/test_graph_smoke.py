"""A8 图冒烟（D1-12 约束 3 + R-14 冻结契约）：LangGraph 编排层能编译、能跑通一轮 dry-run 得 PASS。

这条是 `p0-plan §4/A8` 的验收命令本体：`python -m pytest tests/test_graph_smoke.py`。

放在 tests/ 下就必然要面对「p0/pipeline 还没交付」的窗口期：此时 `build_graph` 不存在。
处理原则 —— 本文件 skip（不把台架本身拖红），但 `scripts/verify_p0.py` 的 A8 断言会把
skip 判成 **FAIL**：skip 不等于通过，验收照样不给绿。

R-14 已把图接口冻结成：
    build_graph(*, store, llm, executor, workdir=".", max_attempts=3,
                dry_run=True, injection="none", checkpointer=None)
    invoke({"round_no", "design_text", "design_ref"}) -> dict
    终止态含 state/tickets/task_package/exec_result/gate/snapshot/message
    A8 断言：可编译；round_no==1、tickets 非空、task_package is not None、gate.state=="pass"；
    **不断言 snapshot 的内容**（依赖 git 标签环境）。
"""

from __future__ import annotations

import importlib
import inspect
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

R14_NODES = ("gen_tickets", "guard_tickets", "build_package", "execute", "gate", "finalize")
R14_FINAL_KEYS = ("state", "tickets", "task_package", "exec_result", "gate", "snapshot", "message")


def _load(mod_name: str):
    try:
        return importlib.import_module(mod_name)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{mod_name} 未交付：{type(exc).__name__}: {exc}")


def _build_graph(pipeline):
    build_graph = getattr(pipeline, "build_graph", None)
    if build_graph is None:
        pytest.skip("build_graph 未交付（D1-12）：A8 由 scripts/verify_p0.py 判 FAIL")
    return build_graph


def _gate_state(gate) -> str:
    if gate is None:
        return ""
    if isinstance(gate, dict):
        value = gate.get("state")
    else:
        value = getattr(gate, "state", None)
    return str(getattr(value, "value", value) or "").lower()


def test_build_graph_signature_matches_r14():
    pipeline = _load("kfpa.pipeline")
    build_graph = _build_graph(pipeline)

    sig = inspect.signature(build_graph)
    params = sig.parameters
    kinds = {p.kind for p in params.values()}
    assert kinds <= {inspect.Parameter.KEYWORD_ONLY}, "R-14：build_graph 必须是纯关键字参数"
    for name in ("store", "llm", "executor"):
        assert name in params, f"R-14：build_graph 缺必填参数 `{name}`"
        assert params[name].default is inspect.Parameter.empty, f"`{name}` 必须无默认值"
    for name in ("workdir", "max_attempts", "dry_run", "injection", "checkpointer"):
        assert name in params, f"R-14：build_graph 缺参数 `{name}`"

    assert getattr(pipeline, "GRAPH_NODES", None) == R14_NODES, \
        f"R-14：GRAPH_NODES 必须等于 {R14_NODES}"
    assert hasattr(pipeline, "RoundGraphState"), "R-14：缺 RoundGraphState 类型"
    assert hasattr(pipeline, "run_round"), "D1-12 约束 1：纯函数核心 run_round 必须保留"


def test_build_graph_smoke_dry_run_passes(tmp_path):
    pipeline = _load("kfpa.pipeline")
    build_graph = _build_graph(pipeline)

    store_mod = importlib.import_module("kfpa.store")
    llm_mod = importlib.import_module("kfpa.llm")
    exec_mod = importlib.import_module("kfpa.executor")

    db = tmp_path / "graph-smoke.db"
    work = tmp_path / "runs" / "round-1"
    work.mkdir(parents=True, exist_ok=True)
    store = store_mod.Store(str(db))
    store.init_schema()
    llm = llm_mod.build_llm(llm_mod.load_roles(), provider="mock")
    executor = exec_mod.MockExecutor()

    design = ROOT / "examples/design-r1.md"
    assert design.exists(), "缺 examples/design-r1.md（D1-7 机读行）"
    design_text = design.read_text(encoding="utf-8", errors="replace")

    graph = build_graph(store=store, llm=llm, executor=executor, workdir=str(work),
                        max_attempts=3, dry_run=True, injection="none")
    app = graph.compile() if hasattr(graph, "compile") else graph
    assert hasattr(app, "invoke"), "build_graph() 的返回值既不是图也不是可调用图"

    result = app.invoke({"round_no": 1, "design_text": design_text,
                         "design_ref": "examples/design-r1.md"})
    assert isinstance(result, dict), f"R-14：invoke 必须返回 dict，实际 {type(result).__name__}"
    for key in R14_FINAL_KEYS:
        assert key in result, f"R-14：终止态缺键 `{key}`"
    assert result["round_no"] == 1
    assert result["tickets"], "R-14：tickets 必须非空"
    assert result["task_package"] is not None, "R-14：task_package 必须非空"
    assert _gate_state(result["gate"]) == "pass", \
        f"R-14：gate.state 应为 pass，实际 {_gate_state(result['gate'])!r}"

    # 图跑完必须真的落库（图只是编排层，台账仍在自己 SQLite）
    con = sqlite3.connect(str(db))
    try:
        snaps = con.execute("select count(*) from snapshots").fetchone()[0]
        gates = con.execute("select state from gate_reports").fetchall()
    finally:
        con.close()
    assert any(str(g[0]).lower() == "pass" for g in gates), f"gate_reports = {gates}"
    assert snaps >= 1, "图跑完 snapshots 应有行（本机无 git 标签时不强断言 tag）"

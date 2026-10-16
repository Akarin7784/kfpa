"""P3 `p3/store` 的实现方桩（R-30：实现方桩落 `benchmarks/**`，由镜鉴搬进 `tests/**`）。

覆盖对象（D5-3.2 / D5-3.3 里归我的一侧）：

1. `store/sqlite.py` 的 P3 新方法逐分支：登记（新建 / pending 覆盖 / **已决不覆盖**）、
   查询（`get` / `list` 两种过滤 / 稳定排序）、承认（幂等、`ESCALATED`/`REJECTED` 不可翻白、
   `batch_id` 同批共享、缺行 `KeyError`）、抽检留痕（append-only、`sampled_at` 兜底、坏 JSON 容错）。
2. `store/__init__.py`：`P3_SCHEMA_VERSION` / `P3_TABLE_NAMES` 导出、`StoreProtocol` 新增签名可运行时检查。
3. 双驱动**同签名对拍**：`KFPA_DB_URL`/`KFPA_PG_PORT` 有真 PG 时跑同一条剧本，两边记录逐字段一致
   （`store/postgres.py` 按 R-32 不算 A20 门槛，但 A12/A26 要过，故此处默认尝试、无 PG 则显式 SKIP）。

跑法（干净环境）::

    $env:PYTHONPATH="src"
    python -m pytest benchmarks/p3_store/stub_p3_store.py -q

有 PG 时::

    $env:KFPA_DB_URL="postgresql://kfpa:kfpa@127.0.0.1:55440/kfpa"
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

def _repo_root() -> Path:
    """仓库根（**搬运安全**）：从 pyproject.toml 往上找，不写死层级。

    `parents[2]` 只在 `benchmarks/p3_store/` 下成立；搬进 `tests/` 后层级少一层、
    会指到仓库外（界面与镜鉴都在这上面栽过）。这里改成向上找 `pyproject.toml`，两个位置都对。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[2]


REPO_ROOT = _repo_root()
sys.path.insert(0, str(REPO_ROOT / "src"))

import kfpa.store as st  # noqa: E402
from kfpa.schemas import FastlaneRequest, FastlaneStatus, KnowledgeSample  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_db_url(monkeypatch):
    """A20 是 clean env：本机残留 KFPA_DB_URL 不许把这些 SQLite 用例顶成 PG（R-25.1/R-25.3）。"""
    monkeypatch.delenv(st.DB_URL_ENV, raising=False)
    yield


def _req(rid: str, round_no: int = 1, status: FastlaneStatus = FastlaneStatus.PENDING,
         **kw) -> FastlaneRequest:
    base = dict(
        request_id=rid,
        round_no=round_no,
        summary=f"微型规格 {rid}",
        diff_files=["src/kfpa/x.py"],
        touched_modules=["x"],
        status=status,
    )
    base.update(kw)
    return FastlaneRequest(**base)


# ---------------------------------------------------------------- 常量与守卫（R-20/R-21）

def test_p3_constants_and_schema_version():
    """D5-3.1：schema 升 4，P0/P1/P2 常量不动，P3 表名单冻结。"""
    assert st.SCHEMA_VERSION == st.P3_SCHEMA_VERSION == "4"
    assert (st.P0_SCHEMA_VERSION, st.P1_SCHEMA_VERSION, st.P2_SCHEMA_VERSION) == ("1", "2", "3")
    assert st.P3_TABLE_NAMES == ("fastlane_requests", "knowledge_samples")
    assert set(st.ALL_TABLE_NAMES) == set(st.TABLE_NAMES) | {
        "ledger", "knowledge", "reviews", "fastlane_requests", "knowledge_samples"}
    store = st.Store(":memory:")
    assert store.get_meta("schema_version") == "4"
    assert set(st.ALL_TABLE_NAMES) <= set(store.list_tables())
    store.close()


def test_protocol_declares_p3_signatures():
    """`StoreProtocol` 必须声明 P3 六个方法（否则 PG 驱动漏实现也不会被发现）。"""
    store = st.Store(":memory:")
    assert isinstance(store, st.StoreProtocol)
    for name in ("add_fastlane_request", "get_fastlane_request", "update_fastlane_request",
                 "list_fastlane_requests", "ack_fastlane_request", "add_knowledge_sample",
                 "list_knowledge_samples"):
        assert callable(getattr(store, name, None)), name
    store.close()


# ---------------------------------------------------------------- 登记语义

def test_add_fastlane_request_roundtrip_and_json_lists():
    store = st.Store(":memory:")
    got = store.add_fastlane_request(_req("fl-1-1", diff_files=["a.py", "b.py"],
                                          touched_modules=["m1"]))
    assert got.request_id == "fl-1-1" and got.round_no == 1
    assert got.diff_files == ["a.py", "b.py"] and got.touched_modules == ["m1"]
    assert got.status is FastlaneStatus.PENDING and got.decided_at == 0.0
    assert got.new_interface is False and got.new_data_model is False
    assert got.judged_fastlane() is True

    fresh = store.get_fastlane_request("fl-1-1")
    assert fresh == got
    assert store.get_fastlane_request("nope") is None
    store.close()


def test_add_fastlane_request_is_idempotent_for_pending():
    """重跑 plan：pending 行可被覆盖（幂等重跑），但不新增行。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1", summary="旧"))
    store.add_fastlane_request(_req("fl-1-1", summary="新"))
    rows = store.list_fastlane_requests()
    assert len(rows) == 1 and rows[0].summary == "新"
    store.close()


def test_replan_never_clobbers_decided_row():
    """A23 核心：已决行的 status/batch_id/decided_by/decided_at 是审计留痕，重跑 plan 不许抹。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1", summary="旧"))
    acked = store.ack_fastlane_request("fl-1-1", by="panshi", batch_id="b-1")
    assert acked.status is FastlaneStatus.APPROVED

    store.add_fastlane_request(_req("fl-1-1", summary="重跑"))
    again = store.get_fastlane_request("fl-1-1")
    assert again.summary == "旧"                       # 覆盖被 WHERE 挡住
    assert again.status is FastlaneStatus.APPROVED
    assert again.batch_id == "b-1" and again.decided_by == "panshi"
    assert again.decided_at == acked.decided_at
    store.close()


def test_list_fastlane_requests_filters_and_order():
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-2-2", round_no=2))
    store.add_fastlane_request(_req("fl-1-2", round_no=1))
    store.add_fastlane_request(_req("fl-1-1", round_no=1))
    store.add_fastlane_request(_req("fl-1-3", round_no=1, status=FastlaneStatus.ESCALATED))

    assert [r.request_id for r in store.list_fastlane_requests()] == [
        "fl-1-1", "fl-1-2", "fl-1-3", "fl-2-2"]
    assert [r.request_id for r in store.list_fastlane_requests(round_no=1)] == [
        "fl-1-1", "fl-1-2", "fl-1-3"]
    # status 同时接受枚举与裸字符串（str Enum）
    assert [r.request_id for r in store.list_fastlane_requests(
        status=FastlaneStatus.PENDING, round_no=1)] == ["fl-1-1", "fl-1-2"]
    assert [r.request_id for r in store.list_fastlane_requests(status="pending")] == [
        "fl-1-1", "fl-1-2", "fl-2-2"]
    assert store.list_fastlane_requests(status="rejected") == []
    store.close()


def test_update_fastlane_request_persists_pending_and_upserts_missing():
    """整对象写回（`fastlane._decide()` 的口）：pending 行整条落库；缺行当保存用。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1"))
    pending = store.get_fastlane_request("fl-1-1")
    pending.summary = "微型规格（改过）"
    pending.status = FastlaneStatus.APPROVED
    pending.decided_by = "panshi"
    pending.decided_at = 123.0
    pending.batch_id = "flb-1-1"
    got = store.update_fastlane_request(pending)
    assert got.status is FastlaneStatus.APPROVED and got.decided_by == "panshi"
    assert got.decided_at == 123.0 and got.batch_id == "flb-1-1"
    assert got.summary == "微型规格（改过）"

    # 缺行 → 整条落库（不新增异常路径）
    fresh = store.update_fastlane_request(_req("fl-1-9", summary="未 plan 过"))
    assert fresh.request_id == "fl-1-9" and fresh.status is FastlaneStatus.PENDING
    store.close()


def test_update_fastlane_request_never_rewrites_decided_row():
    """已决行：同状态幂等返回库里那条（不覆盖首次 decided_by/decided_at）。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1"))
    first = store.ack_fastlane_request("fl-1-1", by="panshi", batch_id="b-1")
    tampered = store.get_fastlane_request("fl-1-1")
    tampered.decided_by = "hacker"
    tampered.decided_at = 999.0
    got = store.update_fastlane_request(tampered)
    assert got.decided_by == "panshi" and got.decided_at == first.decided_at
    assert got.batch_id == "b-1"
    store.close()


def test_update_fastlane_request_refuses_flip_to_approved():
    """A23 兜底：即使调用方绕过模块层状态机，存储层也不许把 escalated/rejected 翻成 approved。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1", status=FastlaneStatus.ESCALATED))
    forged = store.get_fastlane_request("fl-1-1")
    forged.status = FastlaneStatus.APPROVED
    forged.decided_by = "panshi"
    got = store.update_fastlane_request(forged)
    assert got.status is FastlaneStatus.ESCALATED and got.decided_by == ""
    assert store.get_fastlane_request("fl-1-1").status is FastlaneStatus.ESCALATED
    store.close()


# ---------------------------------------------------------------- 承认语义（A23 三条硬断言）

def test_ack_writes_audit_fields_and_sets_time():
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1"))
    acked = store.ack_fastlane_request("fl-1-1", by="panshi", batch_id="batch-a")
    assert acked.status is FastlaneStatus.APPROVED
    assert acked.decided_by == "panshi" and acked.batch_id == "batch-a"
    assert acked.decided_at > 0
    store.close()


def test_ack_is_idempotent_and_keeps_first_decision():
    """重复承认：不重复写行、不覆盖首次 decided_by/decided_at，返回原记录。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1"))
    first = store.ack_fastlane_request("fl-1-1", by="panshi", batch_id="b-1")
    second = store.ack_fastlane_request("fl-1-1", by="someone-else", batch_id="b-2")
    assert second.decided_by == "panshi" and second.batch_id == "b-1"
    assert second.decided_at == first.decided_at
    assert len(store.list_fastlane_requests()) == 1       # 没有第二次写行
    store.close()


def test_batch_ack_shares_batch_id():
    store = st.Store(":memory:")
    for rid in ("fl-1-1", "fl-1-2"):
        store.add_fastlane_request(_req(rid))
    for rid in ("fl-1-1", "fl-1-2"):
        store.ack_fastlane_request(rid, by="panshi", batch_id="batch-2026-10-17")
    got = store.list_fastlane_requests(status=FastlaneStatus.APPROVED)
    assert {r.batch_id for r in got} == {"batch-2026-10-17"}
    store.close()


@pytest.mark.parametrize("frozen", [FastlaneStatus.ESCALATED, FastlaneStatus.REJECTED])
def test_escalated_or_rejected_cannot_be_flipped_to_approved(frozen):
    """A23：`ESCALATED`/`REJECTED` 不可被 ack 翻成 `APPROVED`（要翻必须新开请求）。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1", status=frozen))
    got = store.ack_fastlane_request("fl-1-1", by="panshi", batch_id="b-x")
    assert got.status is frozen
    assert got.decided_by == "" and got.batch_id == "" and got.decided_at == 0.0
    store.close()


def test_reject_and_escalate_transitions_from_pending():
    """pending 行可以被人工判成 rejected/escalated（三态之外的两态是合法终态）。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1"))
    rejected = store.ack_fastlane_request("fl-1-1", by="panshi",
                                          status=FastlaneStatus.REJECTED, note="越界")
    assert rejected.status is FastlaneStatus.REJECTED
    assert rejected.decided_by == "panshi" and rejected.note == "越界"
    assert rejected.escalations() == []          # 判据本身仍全真，人工拒绝是另一回事
    store.close()


def test_ack_missing_request_raises_keyerror_in_chinese():
    store = st.Store(":memory:")
    with pytest.raises(KeyError) as ei:
        store.ack_fastlane_request("fl-9-9")
    assert "快车道请求不存在" in str(ei.value)
    store.close()


def test_escalations_names_failing_criteria():
    """D5-3.2「不许只给布尔」：escalations 指名是哪条判据，且不满足时 judged_fastlane 为假。"""
    req = _req("fl-1-1", new_interface=True, new_data_model=True,
               touched_modules=["m1", "m2"])
    assert req.judged_fastlane() is False
    assert req.escalations() == ["new_interface", "new_data_model", "touched_modules>1"]
    store = st.Store(":memory:")
    store.add_fastlane_request(req)
    stored = store.get_fastlane_request("fl-1-1")
    assert stored.escalations() == ["new_interface", "new_data_model", "touched_modules>1"]
    store.close()


# ---------------------------------------------------------------- 抽检留痕

def test_knowledge_samples_append_only_and_filters():
    store = st.Store(":memory:")
    s1 = store.add_knowledge_sample(KnowledgeSample(cid="K1", sampled_by="panshi",
                                                    rate=0.2, passed=True))
    assert s1.cid == "K1" and s1.passed is True and s1.rate == 0.2 and s1.sampled_at > 0
    store.add_knowledge_sample(KnowledgeSample(cid="K1", sampled_by="panshi",
                                               rate=0.2, passed=False))
    store.add_knowledge_sample(KnowledgeSample(cid="K2", sampled_by="panshi",
                                               rate=0.1, passed=True))
    assert len(store.list_knowledge_samples()) == 3          # append-only：同 cid 多行
    k1 = store.list_knowledge_samples("K1")
    assert [x.passed for x in k1] == [True, False]           # 按 sampled_at, id 稳定排序
    assert [x.cid for x in store.list_knowledge_samples("K2")] == ["K2"]
    assert store.list_knowledge_samples("K9") == []
    store.close()


def test_knowledge_sample_sampled_at_fallback():
    """`sampled_at=0` 由存储层补当前时间（调用方不必自己取 time），避免留痕出现 0 时刻。"""
    store = st.Store(":memory:")
    got = store.add_knowledge_sample(KnowledgeSample(cid="K1", sampled_at=0.0))
    assert got.sampled_at > 0
    store.close()


def test_bad_json_columns_degrade_to_empty_list():
    """坏数据不许炸读路径：手工塞非法 JSON，读回来是空列表（宽容解析口径同 P2 severity）。"""
    store = st.Store(":memory:")
    store.add_fastlane_request(_req("fl-1-1"))
    store._conn.execute("UPDATE fastlane_requests SET diff_files = ?, touched_modules = ? "
                        "WHERE request_id = ?", ("{oops", "not-json", "fl-1-1"))
    store._conn.commit()
    got = store.get_fastlane_request("fl-1-1")
    assert got.diff_files == [] and got.touched_modules == []
    # JSON 合法但不是列表 → 同样退化成空列表
    store._conn.execute("UPDATE fastlane_requests SET diff_files = ? WHERE request_id = ?",
                        (json.dumps({"a": 1}), "fl-1-1"))
    store._conn.commit()
    assert store.get_fastlane_request("fl-1-1").diff_files == []
    store.close()


# ---------------------------------------------------------------- cli 插件组（R-34）

def test_fastlane_group_is_owned_by_fastlane_module():
    """R-34 终态：`fastlane` 组由 `kfpa.fastlane` 独占，六条命令共存，`impact` 不再有 CLI。"""
    import argparse

    import kfpa.cli as cli
    import kfpa.impact as impact

    parser = cli.build_parser()
    root = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][-1]
    assert set(root.choices) >= {"fastlane", "rag", "knowledge", "eval"}
    assert "impact" not in root.choices          # 从来就没有 `kfpa impact` 组，R-34 后也不许有
    assert list(root.choices["fastlane"]._actions[-1].choices) == [
        "check", "plan", "list", "ack", "batch-ack", "show"]
    assert parser.parse_args(["fastlane", "check", "--round", "1"]).fastlane_cmd == "check"
    assert parser.parse_args(["fastlane", "plan", "--round", "1"]).fastlane_cmd == "plan"
    # 镜鉴的抢注探针口径：进程内调 impact.register() 必须不再抢注 `fastlane`
    probe = argparse.ArgumentParser(prog="probe").add_subparsers(dest="command")
    assert impact.register(probe) is None
    assert "fastlane" not in probe.choices
    # `check` 收敛成 `register_check(sub)` 供 owner 复用（挂进调用方给的组）
    owner = argparse.ArgumentParser(prog="kfpa").add_subparsers(dest="command")
    group = owner.add_parser("fastlane")
    impact.register_check(group.add_subparsers(dest="fastlane_cmd", required=True))
    assert list(owner.choices["fastlane"]._actions[-1].choices) == ["check"]


def test_duplicate_plugin_group_is_loud(monkeypatch, capsys):
    """R-34：插件再注册同名组 → 中文报错 + 退出码 2（不许静默并入、不许只警告后继续）。"""
    import argparse
    import importlib
    import types

    import kfpa.cli as cli

    def _first(subparsers):
        subparsers.add_parser("fastlane")

    def _dup(subparsers):
        subparsers.add_parser("fastlane")     # 抢注同名组 → 必须响亮失败

    modules = {}
    for name, register in (("kfpa.fastlane", _first), ("kfpa.rag", _dup)):
        mod = types.ModuleType(name)
        mod.register = register
        modules[name] = mod

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name in modules:
            return modules[name]
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    try:
        parser = argparse.ArgumentParser(prog="kfpa")
        root = parser.add_subparsers(dest="command")
        with pytest.raises(SystemExit) as ei:
            cli._register_plugin_commands(root)
        assert ei.value.code == cli.EXIT_USAGE == 2
        err = capsys.readouterr().err
        assert "错误" in err and "已被注册" in err and "fastlane" in err
    finally:
        monkeypatch.setattr(importlib, "import_module", real_import)


def test_register_plugin_commands_mounts_fastlane_and_rag(monkeypatch):
    """`_register_plugin_commands` 真的把 `kfpa.fastlane` / `kfpa.rag` 挂上去（D5 §1）。"""
    import argparse
    import importlib
    import types

    import kfpa.cli as cli

    def _fastlane(subparsers):
        parser = subparsers.add_parser("fastlane")
        sub = parser.add_subparsers(dest="fastlane_cmd", required=True)
        for name in ("check", "plan", "list", "ack", "batch-ack", "show"):
            sub.add_parser(name)

    def _rag(subparsers):
        sp = subparsers.add_parser("rag")
        sp.add_argument("query", nargs="?", default="")

    modules = {}
    for name, register in (("kfpa.fastlane", _fastlane), ("kfpa.rag", _rag)):
        mod = types.ModuleType(name)
        mod.register = register
        modules[name] = mod

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name in modules:
            return modules[name]
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    try:
        parser = argparse.ArgumentParser(prog="kfpa")
        root = parser.add_subparsers(dest="command")
        cli._register_plugin_commands(root)
        assert {"fastlane", "rag"} <= set(root.choices)
        assert list(root.choices["fastlane"]._actions[-1].choices) == [
            "check", "plan", "list", "ack", "batch-ack", "show"]
        assert parser.parse_args(["rag", "x"]).query == "x"
    finally:
        monkeypatch.setattr(importlib, "import_module", real_import)


# ---------------------------------------------------------------- 双驱动同签名对拍（有 PG 时）

#: 本桩在**共享** PG 上的专用行前缀（含每次运行的随机段）。
#: 共享 55440 上还有 A12/A26 在用 `fastlane_requests`/`knowledge_samples`——
#: 整表 DELETE 会造出「SQLite 有行、PG 空表」的错配假红，所以只碰自己写的行。
_PG_PREFIX = f"stub3-{uuid.uuid4().hex[:8]}-"


def _pg_dsn() -> str:
    env = os.environ.get("KFPA_PG_PORT", "55440")
    return os.environ.get(st.DB_URL_ENV) or f"postgresql://kfpa:kfpa@127.0.0.1:{env}/kfpa"


def _pg_store():
    try:
        return st.make_store(_pg_dsn())
    except Exception as exc:  # 无 Docker / 无 psycopg → 显式 SKIP（报告里可见）
        pytest.skip(f"无可用 PG（{type(exc).__name__}: {exc}）")


def _clean_own_rows(store) -> None:
    """只删本桩自己写的行（按前缀），不动别人的数据。"""
    ph = "%s" if getattr(store, "name", "") == "postgres" else "?"
    for table, col, value in (("fastlane_requests", "request_id", _PG_PREFIX + "%"),
                              ("knowledge_samples", "cid", _PG_PREFIX + "%")):
        store._conn.execute(f"DELETE FROM {table} WHERE {col} LIKE {ph}", (value,))
    try:
        store._conn.commit()
    except Exception:
        pass


def test_pg_driver_same_signature_and_same_semantics():
    """同一条剧本跑 PG，逐字段与 SQLite 对齐（A12/A26 的存储侧前提）。

    隔离口径：两侧都用 `stub3-<run>-` 前缀的 request_id/cid，只比对**自己的行**，
    结束时只删自己的行 —— 共享 PG 上不许清空整表（见 `_PG_PREFIX` 注释）。
    """
    pg = _pg_store()
    if pg.get_meta("schema_version") != st.SCHEMA_VERSION:
        pytest.skip("PG 库 schema 不是当前版本（先跑一次 init）")
    mem = st.Store(":memory:")
    rid1, rid2, cid = f"{_PG_PREFIX}fl-1-1", f"{_PG_PREFIX}fl-1-2", f"{_PG_PREFIX}K1"
    _clean_own_rows(pg)
    try:
        for store in (pg, mem):
            store.add_fastlane_request(_req(rid1, diff_files=["a.py", "b.py"]))
            store.add_fastlane_request(_req(rid2, status=FastlaneStatus.ESCALATED))
            store.ack_fastlane_request(rid1, by="panshi", batch_id="b-1")
            store.ack_fastlane_request(rid2, by="panshi", batch_id="b-1")
            store.ack_fastlane_request(rid1, by="someone-else", batch_id="b-2")   # 幂等
            # 长流 `_decide()` 的整对象写回口，两驱动必须一致
            req = store.get_fastlane_request(rid2)
            req.summary = "改过"
            store.update_fastlane_request(req)
            store.add_knowledge_sample(KnowledgeSample(cid=cid, sampled_at=0.0,
                                                       sampled_by="panshi", rate=0.2, passed=False))

        def snap(store):
            # request_id 归一到序号位，两侧前缀（含随机段）不同，其余逐字段必须一致
            return [
                {k: (str(v).replace(_PG_PREFIX, "") if k == "request_id" else str(v))
                 for k, v in vars(r).items() if k != "decided_at"}
                | {"decided_at_set": r.decided_at > 0}
                for r in store.list_fastlane_requests()
                if str(r.request_id).startswith(_PG_PREFIX)
            ]

        def samples(store):
            return [(s.cid.replace(_PG_PREFIX, ""), s.passed, s.rate, s.sampled_by)
                    for s in store.list_knowledge_samples()
                    if str(s.cid).startswith(_PG_PREFIX)]

        assert snap(pg) == snap(mem)
        assert samples(pg) == samples(mem)
        # PG 侧同样不许翻白 ESCALATED
        escalated = [r for r in pg.list_fastlane_requests(status="escalated")
                     if r.request_id == rid2]
        assert escalated and escalated[0].decided_by == ""
        with pytest.raises(KeyError):
            pg.ack_fastlane_request(f"{_PG_PREFIX}nope")
    finally:
        _clean_own_rows(pg)
        pg.close()
        mem.close()

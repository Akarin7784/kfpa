"""R-30 交接改写：P3 `fastlane.py` / `knowledge.py` / `rag.py`（镜鉴独立复核版）。

来源：长流 `benchmarks/p3/stub_p3_knowledge.py` @ `1ca8587`（129 项，`expect()` 自跑器式）。

**改写口径（R-30）**：

1. 去掉自跑器壳（`expect()`/`PASSED`/`FAILED`/`main()`/`tempfile.TemporaryDirectory()`/
   `sys.path` 引导/`_force_utf8_stdio()`），换成原生 `pytest` 断言与 `tmp_path`/`monkeypatch`/
   `capsys`；路径统一由 `tests/conftest.py` 注入，桩文件不再自己算层级。
2. 断言按**可观察行为**保留：判据/状态机/幂等/冻结出口名与签名/解析三形状/CLI 退出码/
   覆盖率所需的 CLI 分支；失败信息仍带原标签，便于与实现方桩逐条对账。
3. 独立复核与加强（镜鉴加的部分标 `独立`）：`record_sample` 的「抽检不过 → 回避清单」走
   **存储面** `list_avoid_patterns()`（实现把负知识落成 `FailureRecord.avoid_pattern`，不是
   `kind=avoid` 候选）；`hits_for_package` 的确定性不只比两次相等，还断**同一进程内键序稳定**。
4. R-36 之后 `auto`/`local` 缺凭据 → 回退 rc=0；**显式 `--provider dify`** 才 rc=4 —— 本文件
   按这条口径断（CLI 与模块两层都断）。

不改任何产品代码；FakeStore 是内存测试替身，不碰真库。
"""

from __future__ import annotations

import argparse
import builtins
import inspect
import io
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from kfpa import fastlane, impact, knowledge, pipeline, rag
from kfpa.schemas import (
    CandidateStatus,
    FastlaneRequest,
    FastlaneStatus,
    KnowledgeCandidate,
    TaskPackage,
)

# ------------------------------------------------------------------ 测试替身


class FakeStore:
    """按 D5 §3.2/§3.3 的最小 store 替身（方法名与 `store/**` 双驱动一致）。"""

    def __init__(self) -> None:
        self.requests: dict[str, FastlaneRequest] = {}
        self.candidates: dict[str, KnowledgeCandidate] = {}
        self.samples: list = []
        self.failures: list = []
        self.packages: dict[int, TaskPackage] = {}
        self.avoid: list[str] = []

    # ---- fastlane_requests
    def add_fastlane_request(self, request: FastlaneRequest) -> FastlaneRequest:
        self.requests[request.request_id] = request
        return request

    def get_fastlane_request(self, request_id: str):
        return self.requests.get(request_id)

    def list_fastlane_requests(self, status=None, round_no=None):
        items = list(self.requests.values())
        if status:
            items = [r for r in items if r.status.value == status]
        if round_no is not None:
            items = [r for r in items if int(r.round_no) == int(round_no)]
        return sorted(items, key=lambda r: (int(r.round_no), str(r.request_id)))

    def update_fastlane_request(self, request: FastlaneRequest) -> FastlaneRequest:
        self.requests[request.request_id] = request
        return request

    def ack_fastlane_request(self, request_id: str, *, by="", status=FastlaneStatus.APPROVED,
                             batch_id="", note="", now=None) -> FastlaneRequest:
        """冻结写入口：幂等 + 不可翻案在存储层。"""
        row = self.requests.get(request_id)
        if row is None:
            raise KeyError(f"快车道请求不存在：{request_id}")
        if row.decided_by:                      # 已决行原样返回，不抹首次留痕
            return row
        row.status = status
        row.decided_by = str(by or "") or "unknown"
        row.decided_at = float(now if now is not None else 999.0)
        if batch_id:
            row.batch_id = str(batch_id)
        if note:
            row.note = str(note)
        return row

    # ---- knowledge
    def add_candidate(self, candidate: KnowledgeCandidate) -> None:
        self.candidates[candidate.cid] = candidate

    def get_candidate(self, cid: str):
        return self.candidates.get(cid)

    def list_candidates(self, status=None):
        items = list(self.candidates.values())
        if status:
            items = [c for c in items if c.status.value == status]
        return items

    def promote_candidate(self, cid: str, reused_by_round: int):
        candidate = self.candidates[cid]
        candidate.status = CandidateStatus.PROMOTED
        if reused_by_round not in candidate.reused_by:
            candidate.reused_by.append(reused_by_round)
        return candidate

    def drop_candidate(self, cid: str, reason: str) -> None:
        self.candidates[cid].status = CandidateStatus.DROPPED
        self.drop_reason = (cid, reason)

    def add_knowledge_sample(self, sample) -> None:
        self.samples.append(sample)

    def list_knowledge_samples(self, cid=None):
        return [s for s in self.samples if cid is None or s.cid == cid]

    def record_failure(self, failure) -> None:
        self.failures.append(failure)
        if failure.avoid_pattern:
            self.avoid.append(failure.avoid_pattern)

    def list_avoid_patterns(self, round_no=None):
        if round_no is None:
            return sorted(set(self.avoid))
        return sorted({f.avoid_pattern for f in self.failures
                       if f.avoid_pattern and int(f.round_no) == int(round_no)})

    # ---- 任务包
    def get_package(self, round_no: int):
        return self.packages.get(int(round_no))

    def save_package(self, package: TaskPackage) -> str:
        self.packages[int(package.round_no)] = package
        return "fake-hash"


class FakeStoreLegacy(FakeStore):
    """没有 `ack_fastlane_request` 的老库：模块应退回本地改写 + `update_fastlane_request`。"""

    ack_fastlane_request = None


class FakeStoreStrictAck(FakeStore):
    """磐石真实签名：`ack_fastlane_request(id, *, by, status, batch_id, note)` **没有 now**。"""

    def ack_fastlane_request(self, request_id, *, by="", status=FastlaneStatus.APPROVED,
                             batch_id="", note=""):
        row = self.requests.get(request_id)
        if row is None:
            raise KeyError(f"快车道请求不存在：{request_id}")
        if row.decided_by:
            return row
        row.status = status
        row.decided_by = by or "unknown"
        row.decided_at = 1234.0                # 审计时点归存储层
        if batch_id:
            row.batch_id = batch_id
        if note:
            row.note = note
        return row


def make_cli(order="fastlane_only"):
    """按 `cli.py` 的挂载方式建解析器。

    `fastlane_only` = **R-34 终态**（只有 `kfpa.fastlane` 注册 `fastlane`）；
    另两种顺序用于证明「注册顺序无关、六条命令不丢」。
    """
    parser = argparse.ArgumentParser(prog="kfpa")
    sub = parser.add_subparsers(dest="command")
    modules = [fastlane, knowledge, rag]
    if order == "impact_first":
        modules = [impact, fastlane, knowledge, rag]
    if order == "fastlane_first":
        modules = [fastlane, impact, knowledge, rag]
    for module in modules:
        try:
            module.register(sub)
        except Exception:  # noqa: BLE001 - cli.py 真实行为：坏插件不拖垮 CLI
            pass
    return parser


def fastlane_group_names(parser) -> list[str]:
    group = parser._subparsers._group_actions[0]._name_parser_map["fastlane"]
    return sorted(group._subparsers._group_actions[0]._name_parser_map)


def call_cli(parser, argv: list[str], store: FakeStore, module) -> int:
    """跑 CLI handler；store 注入：模块有 `_store_for` 就换它，否则换 `store.make_store`。"""
    import kfpa.store as store_mod

    args = parser.parse_args(argv)
    if hasattr(module, "_store_for"):
        original = module._store_for
        module._store_for = lambda _db: store  # noqa: ARG005 - 桩注入
        try:
            with redirect_stdout(io.StringIO()):
                return args.handler(args)
        finally:
            module._store_for = original
    original_make = store_mod.make_store
    store_mod.make_store = lambda _db=None: store  # noqa: ARG005 - 桩注入（knowledge 用工厂）
    try:
        with redirect_stdout(io.StringIO()):
            return args.handler(args)
    finally:
        store_mod.make_store = original_make


class FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _dify_env_clean(monkeypatch) -> None:
    for key in (rag.DIFY_URL_ENV, rag.DIFY_API_KEY_ENV):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------------ fastlane：判据


def test_judge_true_when_three_criteria_hold():
    ok = FastlaneRequest(request_id="fl-1-1", round_no=1, summary="小改",
                         diff_files=["src/kfpa/a.py"], touched_modules=["kfpa"])
    assert fastlane.judge(ok) is True
    assert fastlane.escalate_reasons(ok) == []


def test_judge_false_names_every_broken_criterion():
    bad = FastlaneRequest(request_id="fl-1-2", round_no=1, summary="动契约",
                          diff_files=["src/kfpa/a.py"],
                          new_interface=True, new_data_model=True,
                          touched_modules=["kfpa", "migrations"])
    assert fastlane.judge(bad) is False
    reasons = fastlane.escalate_reasons(bad)
    assert reasons == ["new_interface", "new_data_model", "touched_modules>1"], reasons


def test_no_diff_source_is_never_fastlane_r35():
    """R-35：空 `diff_files` 不许默认真 —— judge False 且 reasons 指名。"""
    no_source = FastlaneRequest(request_id="fl-1-9", round_no=1, summary="没来源", diff_files=[])
    assert fastlane.judge(no_source) is False
    assert fastlane.escalate_reasons(no_source) == [fastlane.NO_DIFF_SOURCE]


def test_make_request_diff_source_precedence(tmp_path):
    """R-23 三来源优先级：执行产物 → `--files` → `no_diff_source`（不许猜）。"""
    package = TaskPackage(round_no=3, feature="f", goal="g",
                          interface_contract=["新增接口 POST /orders"])
    two_modules = fastlane.make_request(3, 1, "s", package=None,
                                        files=["src/kfpa/a.py", "docs/b.md"])
    assert two_modules.status is FastlaneStatus.ESCALATED
    assert two_modules.escalations() == ["touched_modules>1"]

    single = fastlane.make_request(3, 2, "s", package=None, files=["src/kfpa/a.py"])
    assert single.status is FastlaneStatus.PENDING
    assert single.diff_files == ["src/kfpa/a.py"]

    contract = fastlane.make_request(3, 3, "s", package=package, files=["src/kfpa/a.py"])
    assert contract.status is FastlaneStatus.ESCALATED
    assert contract.escalations() == ["new_interface"]

    empty_dir = tmp_path / "empty"
    nodiff = fastlane.make_request(3, 4, "s", package=None, files=None, workdir=str(empty_dir))
    assert nodiff.status is FastlaneStatus.ESCALATED
    assert nodiff.note == fastlane.NO_DIFF_SOURCE
    assert fastlane.escalate_reasons(nodiff) == [fastlane.NO_DIFF_SOURCE]

    workdir = tmp_path / "r3"
    workdir.mkdir()
    (workdir / "files_changed.json").write_text(
        json.dumps({"files_changed": ["src/kfpa/a.py"]}), encoding="utf-8")
    from_workdir = fastlane.make_request(3, 5, "s", package=None, workdir=str(workdir))
    assert from_workdir.diff_files == ["src/kfpa/a.py"]
    assert from_workdir.status is FastlaneStatus.PENDING


def test_next_seq_and_batch_id_are_recomputable():
    store = FakeStore()
    store.add_fastlane_request(FastlaneRequest(request_id="fl-2-1", round_no=2, summary="a"))
    assert fastlane.next_seq(store, 2) == 2
    assert fastlane.next_batch_id(store, 2) == "flb-2-1"


# ------------------------------------------------------------------ fastlane：状态机


def test_ack_approves_and_records_audit():
    store = FakeStore()
    request = fastlane.make_request(5, 1, "s", package=None, files=["src/kfpa/a.py"])
    fastlane.save_request(store, request)
    first = fastlane.ack(store, "fl-5-1", by="qa", now=100.0)
    assert first.status is FastlaneStatus.APPROVED
    assert first.decided_by == "qa" and first.decided_at == 100.0
    assert bool(first.batch_id)


def test_ack_is_idempotent_and_preserves_first_audit():
    store = FakeStore()
    fastlane.save_request(store, fastlane.make_request(5, 1, "s", package=None,
                                                       files=["src/kfpa/a.py"]))
    first = fastlane.ack(store, "fl-5-1", by="qa", now=100.0)
    second = fastlane.ack(store, "fl-5-1", by="other", now=200.0)
    assert second.decided_by == "qa" and second.decided_at == 100.0
    assert second.batch_id == first.batch_id


def test_escalated_and_rejected_cannot_be_flipped():
    store = FakeStore()
    store.add_fastlane_request(FastlaneRequest(request_id="fl-5-2", round_no=5, summary="动契约",
                                               new_interface=True,
                                               status=FastlaneStatus.ESCALATED))
    with pytest.raises(fastlane.FastlaneConflict) as exc:
        fastlane.ack(store, "fl-5-2", by="qa")
    assert "不许翻成 approved" in str(exc.value)
    assert store.get_fastlane_request("fl-5-2").status is FastlaneStatus.ESCALATED

    store.add_fastlane_request(FastlaneRequest(request_id="fl-5-3", round_no=5, summary="拒绝",
                                               status=FastlaneStatus.REJECTED))
    with pytest.raises(fastlane.FastlaneConflict):
        fastlane.ack(store, "fl-5-3", by="qa")


def test_approved_cannot_be_rejected_afterwards():
    store = FakeStore()
    fastlane.save_request(store, fastlane.make_request(5, 1, "s", package=None,
                                                       files=["src/kfpa/a.py"]))
    approved = fastlane.ack(store, "fl-5-1", by="qa")
    with pytest.raises(fastlane.FastlaneConflict):
        fastlane.reject(store, "fl-5-1", by="qa")
    assert store.get_fastlane_request("fl-5-1").status is approved.status


def test_ack_unknown_request_raises_fastlane_error():
    store = FakeStore()
    with pytest.raises(fastlane.FastlaneError) as exc:
        fastlane.ack(store, "fl-9-9", by="qa")
    assert "查无此单" in str(exc.value)


def test_batch_ack_shares_batch_id_and_reports_failures():
    store = FakeStore()
    for seq in (1, 2, 3):
        store.add_fastlane_request(FastlaneRequest(request_id=f"fl-6-{seq}",
                                                   round_no=6, summary=f"s{seq}"))
    store.add_fastlane_request(FastlaneRequest(request_id="fl-6-4", round_no=6, summary="升级",
                                               status=FastlaneStatus.ESCALATED))
    result = fastlane.batch_ack(store, ["fl-6-1", "fl-6-2", "fl-6-4"], "qa", now=7.0)
    assert isinstance(result, list)                    # R-33 冻结签名：返回 list 子类
    assert len(result) == 2
    assert len({r.batch_id for r in result}) == 1 and bool(result.batch_id)
    assert result.batch_id == "flb-6-1"
    assert len(result.failed) == 1 and "fl-6-4" in result.failed[0]


# ------------------------------------------------------------------ fastlane：清单解析


def test_parse_ack_file_line_json_and_empty_shapes():
    assert fastlane.parse_ack_file("# c\n- fl-7-1\nfl-7-2\nrequest_id: fl-7-2\n") == ["fl-7-1", "fl-7-2"]
    assert fastlane.parse_ack_file('["fl-7-3", "fl-7-4"]') == ["fl-7-3", "fl-7-4"]
    assert fastlane.parse_ack_file('{"requests": [{"request_id": "fl-7-5"}]}') == ["fl-7-5"]
    assert fastlane.parse_ack_file("  \n#x\n") == []


def test_parse_ack_file_frozen_shape():
    """R-34 冻结：`acks: [{request_id, note?}]`。"""
    frozen = ("# KFPA 快车道承认批次\n"
              "acks:\n"
              "  - request_id: fl-7-6\n"
              "    note: 已人工确认\n"
              "  - request_id: fl-7-7\n")
    assert fastlane.parse_ack_file(frozen) == ["fl-7-6", "fl-7-7"]
    assert fastlane.parse_ack_file("request_ids:\n  - fl-7-8\n") == ["fl-7-8"]


def test_parse_ack_file_works_without_pyyaml(monkeypatch):
    """没装 pyyaml 时逐行兜底仍认得冻结形状（R-36 后标量 `acks:` 必须被拒，见台架 A23）。"""
    frozen = ("acks:\n"
              "  - request_id: fl-7-6\n"
              "  - request_id: fl-7-7\n")
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert fastlane.parse_ack_file(frozen) == ["fl-7-6", "fl-7-7"]


def test_micro_spec_renders_and_falls_back(monkeypatch):
    spec = fastlane.micro_spec(FastlaneRequest(request_id="fl-8-1", round_no=8, summary="小改"))
    assert "fl-8-1" in spec and "new_interface" in spec

    original = pipeline._render

    def _boom(*_args, **_kwargs):
        raise RuntimeError("template missing")

    monkeypatch.setattr(pipeline, "_render", _boom)
    try:
        fallback = fastlane.micro_spec(FastlaneRequest(request_id="fl-15-1", round_no=15,
                                                       summary="小改", diff_files=["a.py"]))
    finally:
        monkeypatch.setattr(pipeline, "_render", original)
    assert "fl-15-1" in fallback and "a.py" in fallback


def test_escalate_reasons_delegates_to_contract():
    plain = FastlaneRequest(request_id="fl-15-2", round_no=15, summary="s",
                            diff_files=["a.py"], new_interface=True)
    assert fastlane.escalate_reasons(plain) == ["new_interface"]
    explain = fastlane.escalate_reasons(FastlaneRequest(request_id="fl-15-3", round_no=15,
                                                        summary="s",
                                                        note=fastlane.NO_DIFF_SOURCE))
    assert explain == [fastlane.NO_DIFF_SOURCE]


# ------------------------------------------------------------------ fastlane：CLI


@pytest.mark.parametrize("order", ["fastlane_only", "impact_first", "fastlane_first"])
def test_fastlane_group_has_six_subcommands_in_every_mount_order(order):
    """R-34：组唯一 owner 是 `kfpa.fastlane`；换挂载顺序也不许丢 `check`（R-24.1）。"""
    names = fastlane_group_names(make_cli(order))
    assert names == ["ack", "batch-ack", "check", "list", "plan", "show"], names


def test_fastlane_cli_usage_and_conflict_exit_codes(tmp_path):
    parser = make_cli("fastlane_only")
    store = FakeStore()
    assert call_cli(parser, ["fastlane", "ack", "nope", "--by", "qa"], store,
                    fastlane) == fastlane.EXIT_USAGE
    store.add_fastlane_request(FastlaneRequest(request_id="fl-9-1", round_no=9, summary="升级",
                                               status=FastlaneStatus.ESCALATED))
    assert call_cli(parser, ["fastlane", "ack", "fl-9-1", "--by", "qa"], store,
                    fastlane) == fastlane.EXIT_MANUAL
    absent = tmp_path / "kfpa-absent-acks.yaml"
    assert call_cli(parser, ["fastlane", "batch-ack", "--file", str(absent), "--by", "qa"],
                    store, fastlane) == fastlane.EXIT_USAGE


def test_fastlane_cli_plan_list_ack_show(tmp_path, capsys):
    parser = make_cli("fastlane_only")
    store = FakeStore()
    store.packages[1] = TaskPackage(round_no=1, feature="门店订单", goal="实现可承诺量校验",
                                    acceptance=[], boundaries=["b"], interface_contract=["i"],
                                    prohibitions=["p"])
    original = fastlane._store_for
    fastlane._store_for = lambda _db: store
    try:
        args = parser.parse_args(["fastlane", "plan", "--round", "1",
                                  "--files", "src/kfpa/a.py", "--db", "x"])
        assert args.handler(args) == 0
        out = capsys.readouterr().out
        assert "fl-1-1" in out and "门店订单" in out

        list_args = parser.parse_args(["fastlane", "list", "--db", "x"])
        assert list_args.handler(list_args) == 0
        assert "fl-1-1" in capsys.readouterr().out

        fastlane._store_for = lambda _db: FakeStore()
        assert list_args.handler(list_args) == 0
        assert "无快车道请求" in capsys.readouterr().out

        fastlane._store_for = lambda _db: store
        ack_args = parser.parse_args(["fastlane", "ack", "fl-1-1", "--by", "qa",
                                      "--db", "x", "--json"])
        assert ack_args.handler(ack_args) == 0
        assert '"request_id": "fl-1-1"' in capsys.readouterr().out

        show_args = parser.parse_args(["fastlane", "show", "fl-1-1", "--db", "x"])
        assert show_args.handler(show_args) == 0
        assert "微型规格" in capsys.readouterr().out

        show_json = parser.parse_args(["fastlane", "show", "fl-1-1", "--db", "x", "--json"])
        assert show_json.handler(show_json) == 0
        assert "micro_spec" in capsys.readouterr().out

        missing = parser.parse_args(["fastlane", "show", "nope", "--db", "x"])
        assert missing.handler(missing) == fastlane.EXIT_USAGE
    finally:
        fastlane._store_for = original


def test_fastlane_cli_batch_ack_json_and_text(tmp_path, capsys):
    parser = make_cli("fastlane_only")
    store = FakeStore()
    original = fastlane._store_for
    fastlane._store_for = lambda _db: store
    try:
        ack_file = tmp_path / "acks.yaml"
        ack_file.write_text("acks:\n  - request_id: fl-13-1\n  - request_id: fl-13-2\n",
                            encoding="utf-8")
        store.add_fastlane_request(FastlaneRequest(request_id="fl-13-1", round_no=13, summary="s"))
        store.add_fastlane_request(FastlaneRequest(request_id="fl-13-2", round_no=13, summary="升级",
                                                   status=FastlaneStatus.ESCALATED))
        args = parser.parse_args(["fastlane", "batch-ack", "--file", str(ack_file),
                                  "--by", "qa", "--db", "x", "--json"])
        assert args.handler(args) == fastlane.EXIT_MANUAL      # 部分失败 → rc=3
        assert '"failed"' in capsys.readouterr().out

        text_file = tmp_path / "acks2.yaml"
        text_file.write_text("acks:\n  - request_id: fl-13-2\n", encoding="utf-8")
        args2 = parser.parse_args(["fastlane", "batch-ack", "--file", str(text_file),
                                   "--by", "qa", "--db", "x"])
        assert args2.handler(args2) == fastlane.EXIT_MANUAL
        assert "✗" in capsys.readouterr().out
    finally:
        fastlane._store_for = original


def test_fastlane_cli_check_registered_by_owner(capsys):
    """R-34：`check` 由 `kfpa.fastlane` 注册；无任务包也要 rc=0 + 中文提示。"""
    parser = make_cli("fastlane_only")
    store = FakeStore()
    original = fastlane._store_for
    fastlane._store_for = lambda _db: store
    try:
        store.packages[1] = TaskPackage(round_no=1, feature="f", goal="g",
                                        acceptance=[], boundaries=["b"],
                                        interface_contract=["i"], prohibitions=["p"])
        args = parser.parse_args(["fastlane", "check", "--round", "1", "--db", "x"])
        assert args.handler(args) == 0
        assert "轮次 1" in capsys.readouterr().out

        js = parser.parse_args(["fastlane", "check", "--round", "1",
                                "--files", "src/kfpa/a.py", "--db", "x", "--json"])
        assert js.handler(js) == 0
        assert '"fast_lane"' in capsys.readouterr().out

        fastlane._store_for = lambda _db: FakeStore()
        empty = parser.parse_args(["fastlane", "check", "--round", "9", "--db", "x"])
        assert empty.handler(empty) == 0
        assert "还没有任务包" in capsys.readouterr().out
    finally:
        fastlane._store_for = original


def test_fastlane_cli_check_reports_escalation_but_keeps_rc0(capsys):
    """R-34 + R-24.6：`fast_lane=False` 要提示「升级回完整设计回合」且 rc=0。"""
    parser = make_cli("fastlane_only")
    store = FakeStore()
    store.packages[1] = TaskPackage(round_no=1, feature="f", goal="g", acceptance=[],
                                    boundaries=["b"], interface_contract=["i"], prohibitions=["p"])
    original = fastlane._store_for
    fastlane._store_for = lambda _db: store
    try:
        args = parser.parse_args(["fastlane", "check", "--round", "1",
                                  "--files", "src/kfpa/schemas.py", "--db", "x"])
        assert args.handler(args) == 0
        assert "升级回完整设计回合" in capsys.readouterr().out
    finally:
        fastlane._store_for = original


def test_reject_three_states():
    store = FakeStore()
    store.add_fastlane_request(FastlaneRequest(request_id="fl-14-1", round_no=14, summary="s"))
    first = fastlane.reject(store, "fl-14-1", by="qa", note="不需要", now=14.0)
    second = fastlane.reject(store, "fl-14-1", by="other", now=15.0)
    assert first.status is FastlaneStatus.REJECTED
    assert second.decided_by == "qa" and second.decided_at == 14.0    # 幂等保留首次

    store.add_fastlane_request(FastlaneRequest(request_id="fl-14-2", round_no=14, summary="s",
                                               status=FastlaneStatus.APPROVED, batch_id="b"))
    with pytest.raises(fastlane.FastlaneConflict):
        fastlane.reject(store, "fl-14-2", by="qa")


def test_ack_prefers_store_write_entry_with_signature_fallbacks():
    """三种存储形态：带 now 的替身 / 磐石真实签名（无 now）/ 老库（无写入口）。"""
    storage = FakeStore()
    storage.add_fastlane_request(FastlaneRequest(request_id="fl-16-1", round_no=16, summary="s"))
    stored = fastlane.ack(storage, "fl-16-1", "qa", now=16.0)
    assert stored.status is FastlaneStatus.APPROVED and stored.decided_at == 16.0
    assert storage.get_fastlane_request("fl-16-1").decided_by == "qa"

    strict = FakeStoreStrictAck()
    strict.add_fastlane_request(FastlaneRequest(request_id="fl-18-1", round_no=18, summary="s"))
    strict_ack = fastlane.ack(strict, "fl-18-1", "qa", now=18.0)
    assert strict_ack.status is FastlaneStatus.APPROVED
    assert strict_ack.decided_at == 1234.0 and strict_ack.decided_by == "qa"

    legacy = FakeStoreLegacy()
    legacy.add_fastlane_request(FastlaneRequest(request_id="fl-17-1", round_no=17, summary="s"))
    legacy_ack = fastlane.ack(legacy, "fl-17-1", "qa", now=17.0)
    assert legacy_ack.status is FastlaneStatus.APPROVED and legacy_ack.decided_at == 17.0


# ------------------------------------------------------------------ knowledge：闸门


def test_threshold_is_pinned_and_env_overridable(monkeypatch):
    assert knowledge.CONFIDENCE_THRESHOLD == 0.80      # R-33：0.6 是笔误
    monkeypatch.setenv(knowledge.THRESHOLD_ENV, "0.9")
    assert knowledge.confidence_threshold() == 0.9
    monkeypatch.setenv(knowledge.THRESHOLD_ENV, "坏值")
    assert knowledge.confidence_threshold() == knowledge.CONFIDENCE_THRESHOLD


def test_admit_gate_boundaries():
    store = FakeStore()
    low = knowledge.make_candidate("K-LOW", 1, "内容", confidence=0.79)
    edge = knowledge.make_candidate("K-EDGE", 1, "内容", confidence=0.80)
    high = knowledge.make_candidate("K-HIGH", 1, "内容", confidence=0.85)
    empty = knowledge.make_candidate("K-EMPTY", 1, "   ", confidence=0.9)
    for candidate in (low, edge, high, empty):
        knowledge.admit(store, candidate)
    assert low.status is CandidateStatus.DROPPED        # 低于门槛直接丢，不进隔离区
    assert edge.status is CandidateStatus.QUARANTINE    # 边界算通过
    assert high.status is CandidateStatus.QUARANTINE
    assert empty.status is CandidateStatus.DROPPED


def test_can_promote_requires_later_round_and_avoid_never_promotes():
    same_round = KnowledgeCandidate(cid="K-SAME", round_no=2, kind="success", content="c",
                                    confidence=0.9, reused_by=[2])
    assert knowledge.can_promote(same_round) is False   # 防自证
    later = KnowledgeCandidate(cid="K-LATER", round_no=2, kind="success", content="c",
                               confidence=0.9, reused_by=[2, 3])
    assert knowledge.can_promote(later) is True
    assert knowledge.later_rounds(later) == [3]
    avoid = KnowledgeCandidate(cid="K-AV", round_no=1, kind="avoid", content="上次失败",
                               confidence=0.99, reused_by=[5])
    assert knowledge.can_promote(avoid) is False


def test_mark_reused_same_round_then_promotes():
    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K1-001", 1, "用事务包住批量写入",
                                                    confidence=0.9))
    same = knowledge.mark_reused(store, "K1-001", 1, passed=True)
    assert same.reused_by == [] and same.status is CandidateStatus.QUARANTINE
    after = knowledge.mark_reused(store, "K1-001", 2, passed=True)
    assert after.status is CandidateStatus.PROMOTED and after.reused_by == [2]


def test_sampling_rate_tiers_and_deterministic_judgement():
    assert [knowledge.sampling_rate(n) for n in (0, 1, 2, 3, 4)] == [0.20, 0.15, 0.10, 0.05, 0.05]
    candidate = knowledge.make_candidate("K1-001", 1, "内容", confidence=0.9)
    assert knowledge.is_sampled(candidate, 0.2) == knowledge.is_sampled(candidate, 0.2)
    assert knowledge.is_sampled(candidate, 0.0) is False
    assert knowledge.is_sampled(candidate, 1.0) is True


def test_pending_sample_queue_and_trace():
    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K2-001", 2, "避免硬编码路径", confidence=0.8))
    queue = knowledge.pending_sample(store)
    assert isinstance(queue, list) and all("sample_rate" in q for q in queue)
    sample = knowledge.sample_candidate(store, "K2-001", by="qa", passed=True, now=5.0)
    assert sample.passed is True and sample.rate == 0.2
    assert len(knowledge.list_samples(store)) == 1
    assert all(q["cid"] != "K2-001" for q in knowledge.pending_sample(store))


def test_sample_failed_drops_and_writes_avoid_pattern():
    """独立复核：负知识落的是 `FailureRecord.avoid_pattern`（存储面），不是 `kind=avoid` 候选。"""
    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K3-001", 3, "别把 key 写进日志",
                                                    confidence=0.9))
    knowledge.sample_candidate(store, "K3-001", by="qa", passed=False, now=6.0)
    assert store.get_candidate("K3-001").status is CandidateStatus.DROPPED
    assert store.list_avoid_patterns(3) == ["别把 key 写进日志"]
    assert store.failures[0].reason_key == knowledge.SAMPLE_FAILED_REASON_KEY


def test_report_keys_and_counts_are_self_consistent():
    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K3-001", 3, "别把 key 写进日志",
                                                    confidence=0.9))
    knowledge.sample_candidate(store, "K3-001", by="qa", passed=False, now=6.0)
    payload = knowledge.report(store)
    assert set(payload) >= {"contract_version", "threshold", "counts", "sampling_rates",
                            "sampled", "samples", "pending_sample", "dropped_cids",
                            "promoted_cids", "avoid_patterns"}
    assert payload["counts"]["dropped"] == len(payload["dropped_cids"]) == 1
    assert "K3-001" in payload["dropped_cids"]
    assert payload["avoid_patterns"] == ["别把 key 写进日志"]


def test_knowledge_cli_ingest_sample_verdict_report(tmp_path, capsys):
    parser = make_cli("fastlane_only")
    store = FakeStore()
    assert call_cli(parser, ["knowledge", "ingest", "--cid", "K9-1", "--round", "9",
                             "--content", "c", "--confidence", "0.1"], store,
                    knowledge) == 0
    assert store.get_candidate("K9-1").status is CandidateStatus.DROPPED
    assert call_cli(parser, ["knowledge", "ingest", "--cid", "K9-2", "--round", "9",
                             "--content", "写事务", "--confidence", "0.9",
                             "--kind", "success"], store, knowledge) == 0
    assert store.get_candidate("K9-2").status is CandidateStatus.QUARANTINE
    assert call_cli(parser, ["knowledge", "sample", "--cid", "K9-2", "--by", "qa"],
                    store, knowledge) == 0
    assert len(store.samples) == 1
    assert call_cli(parser, ["knowledge", "verdict", "K9-2", "--failed", "--by", "qa"],
                    store, knowledge) == 0
    assert store.get_candidate("K9-2").status is CandidateStatus.DROPPED
    assert "写事务" in store.list_avoid_patterns(9)

    out = tmp_path / "knowledge-report.json"
    assert call_cli(parser, ["knowledge", "report", "--out", str(out), "--db", "x"],
                    store, knowledge) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "counts" in payload and "promoted_cids" in payload


def test_knowledge_cli_unknown_cid_raises_not_silent():
    parser = make_cli("fastlane_only")
    store = FakeStore()
    with pytest.raises(knowledge.KnowledgeError):
        call_cli(parser, ["knowledge", "sample", "--cid", "nope", "--by", "qa"], store, knowledge)
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.sample_candidate(store, "nope", by="qa")


# ------------------------------------------------------------------ 冻结出口名（R-33）


def test_frozen_knowledge_exports_and_record_sample_signature():
    for name in ("admit", "record_sample", "sampling_rate", "promotable", "mark_reused",
                 "make_candidate", "is_sampled", "review_sample"):
        assert callable(getattr(knowledge, name, None)), f"knowledge.{name} 必须可调用"
    sig = inspect.signature(knowledge.record_sample)
    params = list(sig.parameters)
    assert params[:3] == ["store", "cid", "passed"], str(sig)
    assert sig.parameters["sampled_by"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["rate"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["rate"].default == 0.0


def test_frozen_fastlane_exports_and_plan_behaviour():
    for name in ("judge", "plan", "ack", "batch_ack"):
        assert callable(getattr(fastlane, name, None)), f"fastlane.{name} 必须可调用"
    store = FakeStore()
    registered = fastlane.plan(store, 11, summary="小改", files=["src/kfpa/a.py"])
    assert isinstance(registered, FastlaneRequest)
    assert registered.request_id == "fl-11-1"
    assert registered.status is FastlaneStatus.PENDING
    assert store.get_fastlane_request("fl-11-1") is not None

    escalated = fastlane.plan(store, 11, summary="动数据模型", files=["src/kfpa/schemas.py"])
    assert escalated.status is FastlaneStatus.ESCALATED
    assert escalated.escalations() == ["new_data_model"]

    approved = fastlane.ack(store, "fl-11-1", "qa", now=11.0)
    assert approved.status is FastlaneStatus.APPROVED and approved.decided_by == "qa"


def test_plan_without_diff_source_never_pends(tmp_path):
    """R-35 模块级反向断言：没有任何 diff 依据时 `plan()` 必须 ESCALATED。"""
    store = FakeStore()
    empty_dir = tmp_path / "no-diff"
    empty_dir.mkdir()
    planned = fastlane.plan(store, 12, summary="没依据", package=None, files=None,
                            workdir=str(empty_dir))
    status = str(getattr(getattr(planned, "status", ""), "value", getattr(planned, "status", "")))
    assert status == "escalated", status
    assert planned.escalations() == [fastlane.NO_DIFF_SOURCE]


def test_record_sample_frozen_signature_effects():
    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K11-1", 11, "内容", confidence=0.9))
    sample = knowledge.record_sample(store, "K11-1", False, sampled_by="qa", rate=0.15)
    assert sample.rate == 0.15 and sample.sampled_by == "qa" and sample.passed is False
    assert store.get_candidate("K11-1").status is CandidateStatus.DROPPED
    assert store.list_avoid_patterns(11) == ["内容"]


def test_cli_verdict_pass_and_fail_flags():
    parser = make_cli("fastlane_only")
    store = FakeStore()
    call_cli(parser, ["knowledge", "ingest", "--cid", "K12-1", "--round", "12",
                      "--content", "c", "--confidence", "0.9"], store, knowledge)
    assert call_cli(parser, ["knowledge", "verdict", "K12-1", "--fail", "--by", "qa"],
                    store, knowledge) == 0
    assert store.get_candidate("K12-1").status is CandidateStatus.DROPPED
    call_cli(parser, ["knowledge", "ingest", "--cid", "K12-2", "--round", "12",
                      "--content", "d", "--confidence", "0.9"], store, knowledge)
    assert call_cli(parser, ["knowledge", "verdict", "K12-2", "--pass", "--by", "qa"],
                    store, knowledge) == 0
    assert store.get_candidate("K12-2").status is CandidateStatus.QUARANTINE


# ------------------------------------------------------------------ rag


def test_mask_secret_and_url_remove_sensitive_parts():
    assert "sk-secret" not in rag.mask_secret("sk-secret-value")
    assert rag.mask_secret("sk-secret-value").startswith("***")
    assert "token=abc" not in rag.mask_url("https://api.example.com/v1?token=abc")
    assert "user:pass" not in rag.mask_url("https://user:pass@api.example.com/v1")


def test_local_search_stable_promoted_first_and_empty_query():
    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K1", 1, "快车道承认落库用事务", confidence=0.9))
    store.get_candidate("K1").status = CandidateStatus.PROMOTED
    knowledge.admit(store, knowledge.make_candidate("K2", 1, "快车道登记序号可复算", confidence=0.9))

    first = rag.local_search("快车道承认", store=store)
    second = rag.local_search("快车道承认", store=store)
    assert [h.summary for h in first.hits] == [h.summary for h in second.hits]
    assert first.hits and first.hits[0].key == "K1"
    assert first.hits[0].summary == "K1#reused=0"
    assert first.provider == rag.PROVIDER_LOCAL and first.fallback is False

    empty = rag.local_search("  ", store=store)
    assert empty.hits == [] and "不编造" in empty.note


def test_local_search_docs_ranking(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "b.md").write_text("快车道承认 同样命中", encoding="utf-8")
    (docs / "a.md").write_text("快车道承认 命中更多 快车道承认", encoding="utf-8")
    result = rag.local_search("快车道承认", docs_root=docs, top_k=5)
    assert [h.key for h in result.hits] == ["a.md", "b.md"]
    assert all(h.summary.startswith("docs/") and "#hits=" in h.summary for h in result.hits)


def test_hits_for_package_deterministic_and_render_section():
    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K1", 1, "快车道承认落库用事务", confidence=0.9))
    package = TaskPackage(round_no=1, feature="快车道承认", goal="承认落库")
    hits_a = rag.hits_for_package(package, store=store)
    hits_b = rag.hits_for_package(package, store=store)
    assert hits_a and hits_a == hits_b
    assert all(isinstance(h, str) and h for h in hits_a)
    # 排序不变量（分数降 → 路径升）在 `test_local_search_docs_ranking` 里按 docs 源专项断，
    # 这里不重复：知识库与 docs 的摘要格式不同，硬套一个序会变成过强断言。

    with_hits = pipeline.render_task_package(
        TaskPackage(round_no=1, feature="f", goal="g", acceptance=[], boundaries=["b"],
                    interface_contract=["i"], prohibitions=["p"], knowledge_hits=hits_a))
    without = pipeline.render_task_package(
        TaskPackage(round_no=1, feature="f", goal="g", acceptance=[], boundaries=["b"],
                    interface_contract=["i"], prohibitions=["p"]))
    assert "## 7. 知识库命中" in with_hits
    assert "## 7." not in without


def test_auto_without_credentials_falls_back_to_local(monkeypatch):
    """R-36：`auto`（默认）缺凭据 → 本地回退 rc=0 档，且降级可见。"""
    _dify_env_clean(monkeypatch)
    store = FakeStore()
    fallback = rag.search("快车道", store=store, top_k=3)
    assert fallback.provider == rag.PROVIDER_LOCAL and fallback.fallback is True
    assert "本地回退" in fallback.note


def test_explicit_dify_without_credentials_raises_and_cli_rc4(monkeypatch):
    """R-36：显式 `--provider dify` 缺凭据才 rc=4 + 中文（不漏 key）。"""
    _dify_env_clean(monkeypatch)
    with pytest.raises(rag.RagDependencyError) as exc:
        rag.search("x", provider=rag.PROVIDER_DIFY)
    message = str(exc.value)
    assert "DIFY_API_KEY" in message and "sk-" not in message

    parser = make_cli("fastlane_only")
    rc = call_cli(parser, ["rag", "search", "快车道", "--provider", "dify", "--db", ":memory:"],
                  FakeStore(), rag)
    assert rc == 4, rc


def test_dify_adapter_parses_records_and_masks_key(monkeypatch):
    store = FakeStore()
    monkeypatch.setenv(rag.DIFY_URL_ENV, "https://dify.example.com/v1/retrieve")
    monkeypatch.setenv(rag.DIFY_API_KEY_ENV, "app-super-secret-key")
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        captured["auth"] = str(request.headers.get("Authorization"))
        captured["url"] = request.full_url
        return FakeResponse(json.dumps({"records": [
            {"score": 0.87, "segment": {"id": "seg-1", "content": "承认落库"}},
            {"score": 0.42, "segment": {"id": "seg-2", "content": "别的"}},
        ]}))

    monkeypatch.setattr(rag.urllib.request, "urlopen", fake_urlopen)
    dify = rag.search("承认", provider=rag.PROVIDER_DIFY, top_k=5)
    assert dify.provider == rag.PROVIDER_DIFY and dify.fallback is False
    assert [h.summary for h in dify.hits][:1] == ["dify:seg-1#score=0.870"]
    assert str(captured["auth"]).startswith("Bearer ")
    assert "app-super-secret-key" not in json.dumps(dify.to_jsonable(), ensure_ascii=False)


def test_dify_http_error_is_rc5_and_masks_key(monkeypatch):
    monkeypatch.setenv(rag.DIFY_URL_ENV, "https://dify.example.com/v1/retrieve")
    monkeypatch.setenv(rag.DIFY_API_KEY_ENV, "app-super-secret-key")

    def fake_http_error(request, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(rag.urllib.request, "urlopen", fake_http_error)
    with pytest.raises(rag.RagError) as exc:
        rag.dify_search("x")
    message = str(exc.value)
    assert "HTTP 401" in message and "app-super-secret-key" not in message

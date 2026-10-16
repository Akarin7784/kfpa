#!/usr/bin/env python3
"""P3 交接桩：`fastlane.py` / `knowledge.py` / `rag.py`（长流 · R-30 实现方桩）。

为什么需要它：`tests/**` 归镜鉴单写（R-30），实现方只能把自己的桩落在 `benchmarks/**`，
由镜鉴按独立验证口径改写成 `tests/test_p3_knowledge.py`。本脚本覆盖 D5 §3.2–§3.5 的
可离线断言面（A23/A24/A25/A27 的纯逻辑那一半），**不含**需要 store 新表与台架的部分。

覆盖清单：

- `fastlane`：三判据纯函数 / `no_diff_source` 不猜 / 三种 diff 来源 / `fl-<round>-<seq>` 与
  `batch_id` 可复算 / `ack` 幂等与 `ESCALATED|REJECTED` 不许翻白（rc=3）/ `reject` 反向保护 /
  `batch-ack` 同批共享批次且单条失败不拖垮整批 / 清单解析三形状 / 微型规格渲染 /
  **CLI 注册顺序无关**（impact 先建组时复用，fastlane 先建时 `check` 由本模块兜底）。
- `knowledge`：门槛 0.6（0.59 dropped / 0.60 quarantine / 0.85 quarantine）/ `avoid` 永不转正 /
  **转正必须后续轮次**（同轮不算，防自证）/ 抽检率分档 / 确定性抽检 / 抽检留痕 /
  `passed=False → dropped + 回避模式` / `report()` 稳定键 / CLI 六个子命令。
- `rag`：本地回退稳定排序（`PROMOTED` 优先）/ 空查询不编造 / 密钥与 URL 打码 /
  未配凭据 rc=4（不裸崩、不漏 key）/ Dify 适配器解析与 HTTP 失败 rc=5 / `provider=local` 降级可见 /
  `hits_for_package` 确定性 + 任务包命中段渲染。

运行（clean env，自带 R-19 stdio 兜底）：

    python benchmarks/p3/stub_p3_knowledge.py     # 退出码 0=全过 / 1=有失败
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from kfpa import fastlane, impact, knowledge, pipeline, rag  # noqa: E402
from kfpa.schemas import (  # noqa: E402
    CandidateStatus,
    FastlaneRequest,
    FastlaneStatus,
    KnowledgeCandidate,
    TaskPackage,
)

PASSED: list[str] = []
FAILED: list[str] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(f"{name} {detail}".strip())
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 桩：内存 store


class FakeStore:
    """按 D5 §3.2/§3.3 的最小 store 桩（方法名与磐石 `p3/store` 的约定一致）。"""

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

    def list_fastlane_requests(self, status=None):
        items = list(self.requests.values())
        if status:
            items = [r for r in items if r.status.value == status]
        return items

    def update_fastlane_request(self, request: FastlaneRequest) -> FastlaneRequest:
        self.requests[request.request_id] = request
        return request

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


def make_cli(order="impact_first"):
    """按 cli.py 的挂载方式建解析器；两种顺序都要能用（见 `_fastlane_group`）。"""
    parser = argparse.ArgumentParser(prog="kfpa")
    sub = parser.add_subparsers(dest="command")
    modules = [impact, fastlane, knowledge, rag]
    if order == "fastlane_first":
        modules = [fastlane, impact, knowledge, rag]
    for module in modules:
        try:
            module.register(sub)
        except Exception:  # noqa: BLE001 - cli.py 的真实行为：坏插件不拖垮 CLI
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


def env_without_dify():
    keys = (rag.DIFY_URL_ENV, rag.DIFY_API_KEY_ENV)
    return {k: os.environ[k] for k in keys if k in os.environ}, keys


# ---------------------------------------------------------------- fastlane


def run_fastlane() -> None:
    print("== fastlane（D5 §3.2 / A23 / A24）")
    ok = FastlaneRequest(request_id="fl-1-1", round_no=1, summary="小改", touched_modules=["kfpa"])
    expect("三判据全真 → judge=True", fastlane.judge(ok) is True)
    expect("全真时无升级原因", fastlane.escalate_reasons(ok) == [])

    bad = FastlaneRequest(request_id="fl-1-2", round_no=1, summary="动契约",
                          new_interface=True, new_data_model=True,
                          touched_modules=["kfpa", "migrations"])
    expect("判据为假 → judge=False", fastlane.judge(bad) is False)
    reasons = fastlane.escalate_reasons(bad)
    expect("升级原因逐条指名（不是布尔）",
           reasons == ["new_interface", "new_data_model", "touched_modules>1"], str(reasons))

    # 三种 diff 来源 + no_diff_source 不猜
    with tempfile.TemporaryDirectory() as tmp:
        package = TaskPackage(round_no=3, feature="f", goal="g",
                              interface_contract=["新增接口 POST /orders"])
        two_modules = fastlane.make_request(3, 1, "s", package=None,
                                            files=["src/kfpa/a.py", "docs/b.md"])
        expect("来源②：多模块 → ESCALATED 且指名 touched_modules>1",
               two_modules.status is FastlaneStatus.ESCALATED
               and two_modules.escalations() == ["touched_modules>1"],
               str(two_modules.escalations()))
        single = fastlane.make_request(3, 2, "s", package=None, files=["src/kfpa/a.py"])
        expect("来源②：单模块且无契约增量 → PENDING",
               single.status is FastlaneStatus.PENDING and single.diff_files == ["src/kfpa/a.py"])
        contract = fastlane.make_request(3, 3, "s", package=package, files=["src/kfpa/a.py"])
        expect("判据①：任务包有契约增量 → ESCALATED 且指名 new_interface",
               contract.status is FastlaneStatus.ESCALATED
               and contract.escalations() == ["new_interface"], str(contract.escalations()))
        nodiff = fastlane.make_request(3, 4, "s", package=None, files=None,
                                       workdir=str(Path(tmp) / "empty"))
        expect("来源③ no_diff_source：ESCALATED + 显式可见 + 不猜",
               nodiff.status is FastlaneStatus.ESCALATED
               and nodiff.note == fastlane.NO_DIFF_SOURCE
               and fastlane.escalate_reasons(nodiff) == [fastlane.NO_DIFF_SOURCE])

        workdir = Path(tmp) / "r3"
        workdir.mkdir()
        (workdir / "files_changed.json").write_text(
            json.dumps({"files_changed": ["src/kfpa/a.py"]}), encoding="utf-8")
        from_workdir = fastlane.make_request(3, 5, "s", package=None, workdir=str(workdir))
        expect("来源①：执行器产出优先",
               from_workdir.diff_files == ["src/kfpa/a.py"]
               and from_workdir.status is FastlaneStatus.PENDING)

    # 登记序号 / 批次号可复算
    store = FakeStore()
    store.add_fastlane_request(FastlaneRequest(request_id="fl-2-1", round_no=2, summary="a"))
    expect("next_seq 可复算", fastlane.next_seq(store, 2) == 2)
    expect("next_batch_id 可复算", fastlane.next_batch_id(store, 2) == "flb-2-1")

    # ack 幂等 + 状态机保护
    store = FakeStore()
    request = fastlane.make_request(5, 1, "s", package=None, files=["src/kfpa/a.py"])
    fastlane.save_request(store, request)
    first = fastlane.ack(store, "fl-5-1", by="qa", now=100.0)
    expect("PENDING → APPROVED 且留痕",
           first.status is FastlaneStatus.APPROVED and first.decided_by == "qa"
           and first.decided_at == 100.0 and bool(first.batch_id))
    second = fastlane.ack(store, "fl-5-1", by="other", now=200.0)
    expect("二次承认幂等：不覆盖首次 decided_by/decided_at/batch_id",
           second.decided_by == "qa" and second.decided_at == 100.0
           and second.batch_id == first.batch_id)

    escalated = FastlaneRequest(request_id="fl-5-2", round_no=5, summary="动契约",
                                new_interface=True, status=FastlaneStatus.ESCALATED)
    store.add_fastlane_request(escalated)
    raised = ""
    try:
        fastlane.ack(store, "fl-5-2", by="qa")
    except fastlane.FastlaneConflict as exc:
        raised = str(exc)
    expect("ESCALATED 不许被 ack 翻白（rc=3）",
           "不许翻成 approved" in raised and store.get_fastlane_request("fl-5-2").status
           is FastlaneStatus.ESCALATED, raised[:60])

    rejected = FastlaneRequest(request_id="fl-5-3", round_no=5, summary="拒绝",
                               status=FastlaneStatus.REJECTED)
    store.add_fastlane_request(rejected)
    try:
        fastlane.ack(store, "fl-5-3", by="qa")
        flipped = True
    except fastlane.FastlaneConflict:
        flipped = False
    expect("REJECTED 不许被 ack 翻白", flipped is False)
    approved = store.get_fastlane_request("fl-5-1")
    try:
        fastlane.reject(store, "fl-5-1", by="qa")
        rejected_after = False
    except fastlane.FastlaneConflict:
        rejected_after = True
    expect("已 APPROVED 不许事后拒绝", rejected_after
           and store.get_fastlane_request("fl-5-1").status is approved.status)

    missing = ""
    try:
        fastlane.ack(store, "fl-9-9", by="qa")
    except fastlane.FastlaneError as exc:
        missing = str(exc)
    expect("查无此单 → FastlaneError（CLI rc=2）", "查无此单" in missing)

    # batch-ack：同批共享 + 单条失败不拖垮
    store = FakeStore()
    for seq in (1, 2, 3):
        store.add_fastlane_request(FastlaneRequest(
            request_id=f"fl-6-{seq}", round_no=6, summary=f"s{seq}"))
    store.add_fastlane_request(FastlaneRequest(
        request_id="fl-6-4", round_no=6, summary="升级", status=FastlaneStatus.ESCALATED))
    done, failed = fastlane.batch_ack(store, ["fl-6-1", "fl-6-2", "fl-6-4"], by="qa", now=7.0)
    expect("批内成功条目共享 batch_id",
           len(done) == 2 and len({r.batch_id for r in done}) == 1 and bool(done[0].batch_id),
           str([r.batch_id for r in done]))
    expect("批内失败条目被记录、其余照常承认",
           len(failed) == 1 and "fl-6-4" in failed[0])

    # 清单解析三形状
    expect("清单：逐行 + 注释 + 去重保序",
           fastlane.parse_ack_file("# c\n- fl-7-1\nfl-7-2\nrequest_id: fl-7-2\n")
           == ["fl-7-1", "fl-7-2"])
    expect("清单：JSON 数组", fastlane.parse_ack_file('["fl-7-3", "fl-7-4"]') == ["fl-7-3", "fl-7-4"])
    expect("清单：JSON 对象 requests",
           fastlane.parse_ack_file('{"requests": [{"request_id": "fl-7-5"}]}') == ["fl-7-5"])
    expect("清单：空文本 → 空", fastlane.parse_ack_file("  \n#x\n") == [])

    spec = fastlane.micro_spec(FastlaneRequest(request_id="fl-8-1", round_no=8, summary="小改"))
    expect("微型规格渲染（含 request_id 与三判据）",
           "fl-8-1" in spec and "new_interface" in spec)

    # CLI：注册顺序无关 + 子命令齐全
    parser = make_cli("impact_first")
    names = fastlane_group_names(parser)
    expect("顺序 A：impact 先注册 → 六子命令齐全",
           names == ["ack", "batch-ack", "check", "list", "plan", "show"], str(names))
    parser_rev = make_cli("fastlane_first")
    names_rev = fastlane_group_names(parser_rev)
    expect("顺序 B：fastlane 先注册 → 仍六子命令（impact 报冲突被 cli 吞掉）",
           names_rev == ["ack", "batch-ack", "check", "list", "plan", "show"], str(names_rev))
    check_args = parser_rev.parse_args(["fastlane", "check", "--round", "1"])
    expect("顺序 B 下 `check` 仍可解析（R-24.3 功能不丢）", check_args.fastlane_cmd == "check")

    # CLI：退出码
    store = FakeStore()
    rc_missing = call_cli(parser, ["fastlane", "ack", "nope", "--by", "qa"], store, fastlane)
    expect("CLI：查无此单 → rc=2", rc_missing == fastlane.EXIT_USAGE, f"rc={rc_missing}")
    store.add_fastlane_request(FastlaneRequest(
        request_id="fl-9-1", round_no=9, summary="升级", status=FastlaneStatus.ESCALATED))
    rc_conflict = call_cli(parser, ["fastlane", "ack", "fl-9-1", "--by", "qa"], store, fastlane)
    expect("CLI：状态冲突 → rc=3", rc_conflict == fastlane.EXIT_MANUAL, f"rc={rc_conflict}")
    rc_file = call_cli(parser, ["fastlane", "batch-ack", "--file",
                                str(Path(tempfile.gettempdir()) / "kfpa-absent-acks.yaml"),
                                "--by", "qa"], store, fastlane)
    expect("CLI：批次清单不存在 → rc=2", rc_file == fastlane.EXIT_USAGE, f"rc={rc_file}")

    with tempfile.TemporaryDirectory() as tmp:
        ack_file = Path(tmp) / "acks.yaml"
        ack_file.write_text("- fl-10-1\n- fl-10-2\n", encoding="utf-8")
        store = FakeStore()
        for seq in (1, 2):
            store.add_fastlane_request(FastlaneRequest(
                request_id=f"fl-10-{seq}", round_no=10, summary=f"s{seq}"))
        rc_ok = call_cli(parser, ["fastlane", "batch-ack", "--file", str(ack_file),
                                  "--by", "qa"], store, fastlane)
        expect("CLI：batch-ack 成功 → rc=0 且同批共享",
               rc_ok == fastlane.EXIT_OK
               and store.get_fastlane_request("fl-10-1").batch_id
               == store.get_fastlane_request("fl-10-2").batch_id)


# ---------------------------------------------------------------- knowledge


def run_knowledge() -> None:
    print("== knowledge（D5 §3.3 / A25）")
    expect("门槛常量 = 0.6（D5 §3.3.1）", knowledge.CONFIDENCE_THRESHOLD == 0.6)
    old = os.environ.get(knowledge.THRESHOLD_ENV)
    try:
        os.environ[knowledge.THRESHOLD_ENV] = "0.9"
        expect("门槛可被环境覆盖", knowledge.confidence_threshold() == 0.9)
        os.environ[knowledge.THRESHOLD_ENV] = "坏值"
        expect("门槛配置非法 → 回落常量（不猜）",
               knowledge.confidence_threshold() == knowledge.CONFIDENCE_THRESHOLD)
    finally:
        if old is None:
            os.environ.pop(knowledge.THRESHOLD_ENV, None)
        else:
            os.environ[knowledge.THRESHOLD_ENV] = old

    store = FakeStore()
    low = knowledge.make_candidate("K-LOW", 1, "内容", confidence=0.59)
    edge = knowledge.make_candidate("K-EDGE", 1, "内容", confidence=0.60)
    high = knowledge.make_candidate("K-HIGH", 1, "内容", confidence=0.85)
    empty = knowledge.make_candidate("K-EMPTY", 1, "   ", confidence=0.9)
    for candidate in (low, edge, high, empty):
        knowledge.admit(store, candidate)
    expect("低于门槛 → 直接 dropped（不进隔离区）",
           low.status is CandidateStatus.DROPPED)
    expect("门槛边界 0.60 → quarantine（>= 算通过）",
           edge.status is CandidateStatus.QUARANTINE)
    expect("过门槛 → quarantine", high.status is CandidateStatus.QUARANTINE)
    expect("空内容 → dropped", empty.status is CandidateStatus.DROPPED)

    # 转正必须「后续轮次」
    same_round = KnowledgeCandidate(cid="K-SAME", round_no=2, kind="success", content="c",
                                    confidence=0.9, reused_by=[2])
    expect("同轮复用 → 不转正（防自证）", knowledge.can_promote(same_round) is False)
    later = KnowledgeCandidate(cid="K-LATER", round_no=2, kind="success", content="c",
                               confidence=0.9, reused_by=[2, 3])
    expect("后续轮次复用 → 可转正", knowledge.can_promote(later) is True)
    expect("后续轮次清单剔除同轮", knowledge.later_rounds(later) == [3])
    avoid = KnowledgeCandidate(cid="K-AV", round_no=1, kind="avoid", content="上次失败",
                               confidence=0.99, reused_by=[5])
    expect("kind=avoid 永不转正", knowledge.can_promote(avoid) is False)

    store = FakeStore()
    candidate = knowledge.make_candidate("K1-001", 1, "用事务包住批量写入", confidence=0.9)
    knowledge.admit(store, candidate)
    same = knowledge.mark_reused(store, "K1-001", 1, passed=True)
    expect("同轮 mark_reused 不写 reused_by",
           same.reused_by == [] and same.status is CandidateStatus.QUARANTINE,
           str(same.reused_by))
    after = knowledge.mark_reused(store, "K1-001", 2, passed=True)
    expect("后续轮 mark_reused → promoted",
           after.status is CandidateStatus.PROMOTED and after.reused_by == [2])

    expect("抽检率分档 max(0.05, 0.20-0.05n)",
           [knowledge.sampling_rate(n) for n in (0, 1, 2, 3, 4)]
           == [0.20, 0.15, 0.10, 0.05, 0.05])
    expect("抽检判定确定性", knowledge.is_sampled(candidate, 0.2)
           == knowledge.is_sampled(candidate, 0.2)
           and knowledge.is_sampled(candidate, 0.0) is False
           and knowledge.is_sampled(candidate, 1.0) is True)

    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K2-001", 2, "避免硬编码路径", confidence=0.8))
    queue = knowledge.pending_sample(store)
    expect("待抽检队列按动态抽样率", isinstance(queue, list) and all("sample_rate" in q for q in queue))
    sample = knowledge.sample_candidate(store, "K2-001", by="qa", passed=True, now=5.0)
    expect("抽检通过 → 写留痕（含抽样率与结论）",
           sample.passed is True and sample.rate == 0.2 and len(knowledge.list_samples(store)) == 1)
    expect("留痕后不再进待抽检队列",
           all(q["cid"] != "K2-001" for q in knowledge.pending_sample(store)))

    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K3-001", 3, "别把 key 写进日志", confidence=0.9))
    knowledge.sample_candidate(store, "K3-001", by="qa", passed=False, now=6.0)
    expect("抽检未通过 → dropped",
           store.get_candidate("K3-001").status is CandidateStatus.DROPPED)
    expect("抽检未通过 → 内容进回避模式清单（负知识闭环）",
           store.list_avoid_patterns(3) == ["别把 key 写进日志"])
    failure = store.failures[0]
    expect("回避模式 reason_key 固定（可被 G5 口径识别）",
           failure.reason_key == knowledge.SAMPLE_FAILED_REASON_KEY)

    payload = knowledge.report(store)
    expect("report 稳定键齐全",
           set(payload) >= {"contract_version", "threshold", "counts", "sampling_rates",
                            "sampled", "samples", "pending_sample", "dropped_cids",
                            "promoted_cids", "avoid_patterns"}, str(sorted(payload)))
    expect("report 计数与清单自洽",
           payload["counts"]["dropped"] == len(payload["dropped_cids"]) == 1
           and "K3-001" in payload["dropped_cids"])

    # CLI 六子命令
    parser = make_cli("impact_first")
    store = FakeStore()
    rc_ingest_low = call_cli(parser, ["knowledge", "ingest", "--cid", "K9-1", "--round", "9",
                                      "--content", "c", "--confidence", "0.1"], store, knowledge)
    expect("CLI：ingest 低置信 → rc=0 且 dropped",
           rc_ingest_low == 0 and store.get_candidate("K9-1").status is CandidateStatus.DROPPED)
    rc_ingest_high = call_cli(parser, ["knowledge", "ingest", "--cid", "K9-2", "--round", "9",
                                       "--content", "写事务", "--confidence", "0.9",
                                       "--kind", "success"], store, knowledge)
    expect("CLI：ingest 过门槛 → quarantine",
           rc_ingest_high == 0 and store.get_candidate("K9-2").status is CandidateStatus.QUARANTINE)
    rc_sample = call_cli(parser, ["knowledge", "sample", "--cid", "K9-2", "--by", "qa"],
                         store, knowledge)
    expect("CLI：sample --cid 留痕 → rc=0", rc_sample == 0 and len(store.samples) == 1)
    rc_verdict = call_cli(parser, ["knowledge", "verdict", "K9-2", "--failed", "--by", "qa"],
                          store, knowledge)
    expect("CLI：verdict --failed → 再留痕 + dropped + 回避模式",
           rc_verdict == 0 and store.get_candidate("K9-2").status is CandidateStatus.DROPPED
           and "写事务" in store.list_avoid_patterns(9))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "knowledge-report.json"
        rc_report = call_cli(parser, ["knowledge", "report", "--out", str(out),
                                      "--db", "x"], store, knowledge)
        payload = json.loads(out.read_text(encoding="utf-8"))
        expect("CLI：report --out 落盘且键稳定",
               rc_report == 0 and out.is_file() and "counts" in payload and "promoted_cids" in payload)
    raised_cli = False
    try:
        call_cli(parser, ["knowledge", "sample", "--cid", "nope", "--by", "qa"], store, knowledge)
    except knowledge.KnowledgeError:
        raised_cli = True
    expect("CLI：sample 未知 cid → 中文 KnowledgeError 冒给入口（不静默成功）", raised_cli)
    raised_missing = False
    try:
        knowledge.sample_candidate(store, "nope", by="qa")
    except knowledge.KnowledgeError:
        raised_missing = True
    expect("sample 未知 cid → 中文 KnowledgeError", raised_missing)


# ---------------------------------------------------------------- rag


class FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_rag() -> None:
    print("== rag（D5 §3.5 / A27）")
    expect("密钥打码不回明文", "sk-secret" not in rag.mask_secret("sk-secret-value")
           and rag.mask_secret("sk-secret-value").startswith("***"))
    expect("URL 打码抹 query/userinfo",
           "token=abc" not in rag.mask_url("https://api.example.com/v1?token=abc")
           and "user:pass" not in rag.mask_url("https://user:pass@api.example.com/v1"))

    store = FakeStore()
    knowledge.admit(store, knowledge.make_candidate("K1", 1, "快车道承认落库用事务", confidence=0.9))
    promoted = store.get_candidate("K1")
    promoted.status = CandidateStatus.PROMOTED
    knowledge.admit(store, knowledge.make_candidate("K2", 1, "快车道登记序号可复算", confidence=0.9))

    first = rag.local_search("快车道承认", store=store)
    second = rag.local_search("快车道承认", store=store)
    expect("本地检索稳定排序（同输入同输出）",
           [h.summary for h in first.hits] == [h.summary for h in second.hits])
    expect("PROMOTED 优先于 QUARANTINE",
           first.hits and first.hits[0].key == "K1", str([h.summary for h in first.hits]))
    expect("摘要串稳定形状", first.hits[0].summary == "K1#reused=0")
    expect("provider=local 且非回退（显式本地）",
           first.provider == rag.PROVIDER_LOCAL and first.fallback is False)
    empty = rag.local_search("  ", store=store)
    expect("空查询不编造命中", empty.hits == [] and "不编造" in empty.note)

    with tempfile.TemporaryDirectory() as tmp:
        docs = Path(tmp) / "docs"
        docs.mkdir()
        (docs / "b.md").write_text("快车道承认 同样命中", encoding="utf-8")
        (docs / "a.md").write_text("快车道承认 命中更多 快车道承认", encoding="utf-8")
        result = rag.local_search("快车道承认", docs_root=docs, top_k=5)
        expect("文档召回稳定排序（分数降 → 路径升）",
               [h.key for h in result.hits] == ["a.md", "b.md"], str([h.key for h in result.hits]))
        expect("文档摘要形状 docs/<rel>#hits=",
               all(h.summary.startswith("docs/") and "#hits=" in h.summary for h in result.hits))

    package = TaskPackage(round_no=1, feature="快车道承认", goal="承认落库")
    hits_a = rag.hits_for_package(package, store=store)
    hits_b = rag.hits_for_package(package, store=store)
    expect("hits_for_package 确定性（进 package_hash 不能漂）", hits_a == hits_b and hits_a != [])
    rendered = pipeline.render_task_package(
        TaskPackage(round_no=1, feature="f", goal="g", acceptance=[], boundaries=["b"],
                    interface_contract=["i"], prohibitions=["p"], knowledge_hits=hits_a))
    expect("任务包渲染：有命中才出现第 7 段",
           "## 7. 知识库命中" in rendered
           and "## 7." not in pipeline.render_task_package(
               TaskPackage(round_no=1, feature="f", goal="g", acceptance=[], boundaries=["b"],
                           interface_contract=["i"], prohibitions=["p"])))

    saved, keys = env_without_dify()
    for key in keys:
        os.environ.pop(key, None)
    try:
        fallback = rag.search("快车道", store=store, top_k=3, docs_root=Path(tempfile.gettempdir()))
        expect("auto：无凭据 → 回退且 provider=local 可见",
               fallback.provider == rag.PROVIDER_LOCAL and fallback.fallback is True
               and "本地回退" in fallback.note)
        raised = False
        try:
            rag.search("x", provider=rag.PROVIDER_DIFY)
        except rag.RagDependencyError as exc:
            raised = True
            message = str(exc)
        expect("显式 dify 无凭据 → RagDependencyError（rc=4 档）", raised)
        expect("凭据缺失消息不漏 key（也没有 key 明文）",
               "DIFY_API_KEY" in message and "sk-" not in message)

        parser = make_cli("impact_first")
        rc = call_cli(parser, ["rag", "search", "快车道", "--provider", "dify",
                               "--db", ":memory:"], store, rag)
        expect("CLI：--provider dify 无凭据 → rc=4", rc == 4, f"rc={rc}")

        os.environ[rag.DIFY_URL_ENV] = "https://dify.example.com/v1/retrieve"
        os.environ[rag.DIFY_API_KEY_ENV] = "app-super-secret-key"
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
            captured["auth"] = str(request.headers.get("Authorization"))
            captured["url"] = request.full_url
            return FakeResponse(json.dumps({"records": [
                {"score": 0.87, "segment": {"id": "seg-1", "content": "承认落库"}},
                {"score": 0.42, "segment": {"id": "seg-2", "content": "别的"}},
            ]}))

        original_open = rag.urllib.request.urlopen
        rag.urllib.request.urlopen = fake_urlopen
        try:
            dify = rag.search("承认", provider=rag.PROVIDER_DIFY, top_k=5)
            expect("Dify 适配器解析 records → provider=dify 且不回退",
                   dify.provider == rag.PROVIDER_DIFY and dify.fallback is False
                   and [h.summary for h in dify.hits][:1] == ["dify:seg-1#score=0.870"],
                   str([h.summary for h in dify.hits]))
            expect("Authorization 走 Bearer 且 key 不出现在返回里",
                   captured["auth"].startswith("Bearer ") and "app-super-secret-key" not in json.dumps(
                       dify.to_jsonable(), ensure_ascii=False))

            def fake_http_error(request, timeout=None):  # noqa: ANN001, ARG001
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

            rag.urllib.request.urlopen = fake_http_error
            message = ""
            try:
                rag.dify_search("x")
            except rag.RagError as exc:
                message = str(exc)
            expect("Dify HTTP 失败 → RagError（rc=5）且不漏 key",
                   "HTTP 401" in message and "app-super-secret-key" not in message, message[:70])
        finally:
            rag.urllib.request.urlopen = original_open
    finally:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(saved)


# ---------------------------------------------------------------- 入口


def _force_utf8_stdio() -> None:
    """R-19：独立脚本不经过 cli.main()，自己做 stdio 兜底。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _force_utf8_stdio()
    print(f"KFPA P3 交接桩（长流 · R-30）  仓库={REPO}")
    print("-" * 72)
    run_fastlane()
    run_knowledge()
    run_rag()
    print("-" * 72)
    print(f"汇总：{len(PASSED) + len(FAILED)} 项 -> PASS {len(PASSED)} / FAIL {len(FAILED)}")
    for item in FAILED:
        print(f"  [FAIL] {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""知識沉淀闸门（P1 D3-3.5 + **P3 D5 §3.3 全量** / design.md §5.5 防知识库中毒）。

规则表（D5 §3.3 + R-33）：

| 阶段 | 规则 |
|---|---|
| 入隔离区 | `confidence >= CONFIDENCE_THRESHOLD`（**0.80**，R-33 裁定维持 D3/A15 原文；D5 v1.0 误写成 0.6）且内容非空；低于门槛或空内容 → 直接 `dropped`，**不进隔离区** |
| 隔离区 | 过门槛的落 `quarantine`；负知识（`kind=avoid`）只走回灌链路，永不转正 |
| 转正 | `can_promote()`：被 **≥1 个后续轮次**（`reused_by` 里的轮次 `> candidate.round_no`）成功复用；**同轮复用不算**（防自证） |
| 抽检 | 抽样率 `max(0.05, 0.20 - 0.05 * 成功复用次数)`；抽中者写 `KnowledgeSample` 留痕；`passed=False` → `DROPPED` **且**内容写进回避模式清单（负知识） |
| 报告 | `report()`：各状态计数 · 抽检率 · 被丢弃清单 · 转正清单（稳定键，A25 断） |

本模块是纯逻辑 + Store 读写，**不调用模型**；抽样用内容哈希做确定性判定（同输入同输出，
台架可复现），不使用 `random`，否则抽检会变成概率性断言。

P3 与 P1 的口径差异（**断言跟口径走**）：

1. 门槛**维持 0.80**（R-33：D5 v1.0 的 0.6 是笔误，A15 期望不回退）；
2. 转正加「后续轮次」约束：同轮复用不再转正（D5 §3.3.2）；
3. 新增抽检留痕与 `report()`（D5 §3.3.3/4），出口名按 R-33 冻结：
   `admit(store, candidate)` / `record_sample(store, cid, passed, *, sampled_by="", rate=0.0)`。
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import TYPE_CHECKING, Any, Optional

from .schemas import (
    CONTRACT_VERSION,
    CandidateStatus,
    FailureRecord,
    KnowledgeCandidate,
    KnowledgeSample,
    TaskPackage,
    dumps,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from .store import Store

#: 入隔离区置信度门槛（**D3 §3.5 / A15 原文 0.80**；R-33 裁定维持，D5 v1.0 的 0.6 是笔误）
CONFIDENCE_THRESHOLD = 0.80
#: 抽检率区间
SAMPLE_RATE_MAX = 0.20
SAMPLE_RATE_MIN = 0.05
SAMPLE_RATE_STEP = 0.05

#: 允许的 kind（success 正知识 / avoid 负知识）
KINDS = ("success", "avoid")

#: 门槛覆盖用的环境变量（D5 §3.3.1「可被 config 覆盖」）
THRESHOLD_ENV = "KFPA_CONFIDENCE_THRESHOLD"

#: 抽检未通过写入回避模式时的 `reason_key`（负知识闭环）
SAMPLE_FAILED_REASON_KEY = "knowledge_sample_failed"


class KnowledgeError(RuntimeError):
    """沉淀闸门可判定错误（中文、三段式）。"""


def confidence_threshold() -> float:
    """当前门槛：默认 `CONFIDENCE_THRESHOLD`，可用 `KFPA_CONFIDENCE_THRESHOLD` 覆盖。

    非法值（非数字 / 越界）→ 回落到常量，**不猜**（配置坏了不能把闸门调成 0）。
    """
    raw = str(os.environ.get(THRESHOLD_ENV) or "").strip()
    if not raw:
        return CONFIDENCE_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return CONFIDENCE_THRESHOLD
    return value if 0.0 <= value <= 1.0 else CONFIDENCE_THRESHOLD


def sampling_rate(successful_reuse_count: int) -> float:
    """抽检率：`max(0.05, 0.20 - 0.05 * n)`，n<0 视为 0（D3-3.5）。

    结果保留 4 位小数，避免二进制浮点残差（`0.20-0.05` 会得到 0.15000000000000002），
    保证同一输入在任何机器上得到同一个可断言的数值。
    """
    count = max(0, int(successful_reuse_count))
    return round(max(SAMPLE_RATE_MIN, SAMPLE_RATE_MAX - SAMPLE_RATE_STEP * count), 4)


def _unit_interval(value: Any) -> float:
    """把 ['0.9', 0.9, '90%'] 归一成 0.0~1.0，非法值当 0.0（宁可丢弃也不编造）。"""
    try:
        if isinstance(value, str) and value.strip().endswith("%"):
            number = float(value.strip()[:-1]) / 100.0
        else:
            number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:  # 传了 90 这种百分数
        number = number / 100.0
    return min(1.0, max(0.0, number))


def make_candidate(
    cid: str,
    round_no: int,
    content: str,
    *,
    kind: str = "success",
    confidence: float = 0.0,
) -> KnowledgeCandidate:
    """构造候选（不含落库）；`cid` 建议 `K{round}-{seq}`，由调用方给。"""
    return KnowledgeCandidate(
        cid=str(cid),
        round_no=int(round_no),
        kind=str(kind) if str(kind) in KINDS else "success",
        content=str(content or "").strip(),
        confidence=_unit_interval(confidence),
    )


def admit(store: "Store", candidate: KnowledgeCandidate) -> KnowledgeCandidate:
    """入隔离区判定（D5 §3.3.1）：不达标直接 dropped，达标进 quarantine 并落库。

    - 空内容 → `dropped`；
    - `kind=avoid`（负知识）→ `quarantine` 但**永不转正**（只走回灌链路），
      不受正知识门槛约束——负知识的价值在于「记住别再做」，不是「可信度」；
    - 其余：`confidence < confidence_threshold()` → **直接 dropped，不进隔离区**。
    """
    if not candidate.content:
        candidate.status = CandidateStatus.DROPPED
    elif candidate.kind == "avoid":
        candidate.status = CandidateStatus.QUARANTINE
    elif candidate.confidence < confidence_threshold():
        candidate.status = CandidateStatus.DROPPED
    else:
        candidate.status = CandidateStatus.QUARANTINE
    store.add_candidate(candidate)
    return candidate


def can_promote(candidate: KnowledgeCandidate) -> bool:
    """转正判定（D5 §3.3.2）：隔离区 + 被 **≥1 个后续轮次**成功复用；负知识永不转正。

    与 `KnowledgeCandidate.can_promote()`（契约层，只看「有没有被复用」）不同：这里加
    「**后续轮次**」约束——同一轮里自己复用自己的内容不算证据（防自证）。
    """
    if candidate.kind == "avoid":
        return False
    if candidate.status is not CandidateStatus.QUARANTINE:
        return False
    return any(int(r) > int(candidate.round_no) for r in (candidate.reused_by or []))


def promotable(candidate: KnowledgeCandidate) -> bool:
    """`can_promote()` 的兼容别名（P1 起就叫这个名字）。"""
    return can_promote(candidate)


def later_rounds(candidate: KnowledgeCandidate) -> list[int]:
    """被哪些**后续**轮次复用（排序去重；同轮/更早的轮次剔除）。"""
    rounds = {int(r) for r in (candidate.reused_by or []) if int(r) > int(candidate.round_no)}
    return sorted(rounds)


def mark_reused(
    store: "Store", cid: str, round_no: int, *, passed: bool
) -> KnowledgeCandidate:
    """标记候选在某轮被复用；**只有 PASS 轮**才计入 `reused_by` 并触发转正。

    D5 §3.3.2：`round_no <= candidate.round_no`（同轮或更早）**不计入**——同轮复用是
    自我引用，不能当转正证据。返回值始终是库里最新的那条候选。
    """
    candidate = store.get_candidate(cid)
    if candidate is None:
        raise KnowledgeError(
            f"[knowledge] 候选不存在：{cid} → 先用 `kfpa knowledge list` 查 cid"
        )
    reuse_round = int(round_no)
    if reuse_round <= int(candidate.round_no):
        return candidate  # 同轮自证不算（D5 §3.3.2）
    if passed and reuse_round not in candidate.reused_by:
        candidate.reused_by.append(reuse_round)
        store.add_candidate(candidate)  # 覆盖式写回
        if promotable(candidate):
            return store.promote_candidate(cid, reuse_round)
    return candidate


def reuse_from_package(
    store: "Store",
    package: TaskPackage,
    round_no: int,
    *,
    passed: bool,
) -> list[str]:
    """把「本轮任务包真的用到的候选」标记为复用（P1 最小闭环，可实证）。

    认定口径：候选 `content` 出现在任务包的 `feedback` 或 `prohibitions` 条目里。
    转正需要**后续轮次**的复用（D5 §3.3.2），所以当轮使用只留痕、不当场转正。
    """
    promoted: list[str] = []
    if not passed:
        return promoted
    haystack = "\n".join(
        [str(x) for x in (package.feedback or [])] + [str(x) for x in (package.prohibitions or [])]
    )
    if not haystack:
        return promoted
    for candidate in store.list_candidates():
        if candidate.kind == "avoid":
            continue  # 负知识不进正区，只留在回灌链路上
        if candidate.status is not CandidateStatus.QUARANTINE:
            continue
        if candidate.content and candidate.content in haystack:
            updated = mark_reused(store, candidate.cid, round_no, passed=True)
            if updated.status is CandidateStatus.PROMOTED:
                promoted.append(updated.cid)
    return promoted


def is_sampled(candidate: KnowledgeCandidate, rate: float) -> bool:
    """确定性抽检：`sha1(cid)` 归一值 < rate 即抽中（同输入同输出，可复现）。"""
    digest = hashlib.sha1(str(candidate.cid).encode("utf-8")).hexdigest()[:8]
    return (int(digest, 16) % 10000) / 10000.0 < max(0.0, min(1.0, float(rate)))


def review_sample(store: "Store", candidates: Optional[list[KnowledgeCandidate]] = None) -> list[dict]:
    """按 D3-3.5 的抽检率给出「要人工抽检」的候选清单（含当前抽样率）。"""
    items = list(candidates if candidates is not None else store.list_candidates())
    out: list[dict] = []
    for candidate in items:
        rate = sampling_rate(len(candidate.reused_by))
        if is_sampled(candidate, rate):
            out.append(
                {
                    "cid": candidate.cid,
                    "kind": candidate.kind,
                    "status": candidate.status.value,
                    "confidence": candidate.confidence,
                    "sample_rate": round(rate, 4),
                }
            )
    return out


# ---------------------------------------------------------------- P3：抽检留痕与结论


def list_samples(store: "Store", cid: Optional[str] = None) -> list[KnowledgeSample]:
    """读抽检留痕（store 缺方法 → 空清单，不崩）。"""
    fn = getattr(store, "list_knowledge_samples", None)
    if not callable(fn):
        return []
    items = fn(cid) or []
    return sorted(
        [x for x in items if isinstance(x, KnowledgeSample)],
        key=lambda s: (str(s.cid), float(s.sampled_at), str(s.sampled_by)),
    )


def pending_sample(store: "Store") -> list[dict]:
    """待抽检队列：按抽样率被抽中、且**尚无留痕**的候选（D5 §3.3.3）。"""
    sampled = {s.cid for s in list_samples(store)}
    return [row for row in review_sample(store) if str(row["cid"]) not in sampled]


def record_sample(
    store: "Store",
    cid: str,
    passed: bool = True,
    *,
    sampled_by: str = "",
    rate: float = 0.0,
    now: Optional[float] = None,
) -> KnowledgeSample:
    """对一条候选留痕并给结论（**R-33 冻结出口名**，A25 按名硬探）。

    - 写 `KnowledgeSample`（`rate` 缺省 0.0 = 用 `sampling_rate(len(reused_by))` 现场算，
      便于回看当时口径）；
    - `passed=False` → 该候选 `DROPPED` **且**内容写进回避模式清单（负知识闭环）。

    抽检是**人工动作**，所以不要求 `is_sampled()` 命中（显式给 `cid` 即强制抽样）；
    未给 `cid` 的「按率列队列」走 `pending_sample()`。
    """
    candidate = store.get_candidate(cid)
    if candidate is None:
        raise KnowledgeError(
            f"[knowledge] 候选不存在：{cid} → 先用 `kfpa knowledge list` 查 cid"
        )
    resolved_rate = float(rate) if float(rate) > 0 else sampling_rate(len(candidate.reused_by))
    sample = KnowledgeSample(
        cid=str(cid),
        sampled_at=float(now if now is not None else time.time()),
        sampled_by=str(sampled_by or "").strip() or "unknown",
        rate=round(resolved_rate, 4),
        passed=bool(passed),
    )
    add = getattr(store, "add_knowledge_sample", None)
    if callable(add):
        add(sample)
    if not passed:
        store.drop_candidate(cid, f"抽检未通过（by={sample.sampled_by}）")
        record = getattr(store, "record_failure", None)
        if callable(record):
            record(
                FailureRecord(
                    round_no=int(candidate.round_no),
                    attempt=1,
                    reason=f"沉淀抽检未通过：{cid}（by={sample.sampled_by}）",
                    avoid_pattern=str(candidate.content),
                    reason_key=SAMPLE_FAILED_REASON_KEY,
                    created_at=sample.sampled_at,
                )
            )
    return sample


def sample_candidate(
    store: "Store",
    cid: str,
    *,
    by: str = "",
    passed: bool = True,
    now: Optional[float] = None,
) -> KnowledgeSample:
    """`record_sample` 的旧名包装（本 PR 内部仍用它，避免改名波及调用点）。"""
    return record_sample(store, cid, passed, sampled_by=by, now=now)


def report(store: "Store") -> dict[str, Any]:
    """闸门报告（D5 §3.3.4，稳定键）：计数 · 抽检 · 丢弃清单 · 转正清单 · 回避模式。"""
    items = list(store.list_candidates())
    counts = {
        CandidateStatus.QUARANTINE.value: 0,
        CandidateStatus.PROMOTED.value: 0,
        CandidateStatus.DROPPED.value: 0,
    }
    rates: dict[str, float] = {}
    for candidate in items:
        counts[candidate.status.value] = counts.get(candidate.status.value, 0) + 1
        rates[candidate.cid] = sampling_rate(len(candidate.reused_by))
    samples = list_samples(store)
    patterns: list[str] = []
    fn = getattr(store, "list_avoid_patterns", None)
    if callable(fn):
        try:
            patterns = [str(p) for p in (fn(None) or []) if str(p).strip()]
        except Exception:  # pragma: no cover - 老库/桩缺该方法
            patterns = []
    return {
        "contract_version": CONTRACT_VERSION,
        "threshold": confidence_threshold(),
        "counts": counts,
        "total": len(items),
        "sampling_rates": rates,
        "sampled": len(samples),
        "samples": [
            {"cid": s.cid, "sampled_at": s.sampled_at, "sampled_by": s.sampled_by,
             "rate": s.rate, "passed": bool(s.passed)}
            for s in samples
        ],
        "pending_sample": [str(row["cid"]) for row in pending_sample(store)],
        "dropped_cids": sorted(c.cid for c in items if c.status is CandidateStatus.DROPPED),
        "promoted_cids": sorted(c.cid for c in items if c.status is CandidateStatus.PROMOTED),
        "avoid_patterns": patterns,
    }


def format_report(candidates: list[KnowledgeCandidate]) -> str:
    """CLI 台账文本（中文，含状态与复用轮次）。"""
    if not candidates:
        return "（无沉淀候选）"
    lines = ["票号        轮次  类型     置信度  状态        成功复用轮次"]
    for c in candidates:
        reused = ",".join(str(r) for r in c.reused_by) or "-"
        lines.append(
            f"{c.cid:<11} {c.round_no:<5} {c.kind:<8} {c.confidence:<7.2f} {c.status.value:<11} {reused}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI 挂载（供 cli.py 调用）


def register(subparsers) -> None:  # pragma: no cover - 由 cli.py 挂载
    """注册 `kfpa knowledge list|promote|ingest|sample|verdict|report`（D3-3.5 + D5 §3.3）。"""
    parser = subparsers.add_parser("knowledge", help="知識沉淀闸门（D3-3.5 / D5 §3.3）")
    sub = parser.add_subparsers(dest="knowledge_cmd", required=True)

    listing = sub.add_parser("list", help="列沉淀候选（可按状态过滤）")
    listing.add_argument("--status", choices=[s.value for s in CandidateStatus], default=None)
    listing.add_argument("--db", default="kfpa.db")
    listing.add_argument("--json", action="store_true")

    promote = sub.add_parser("promote", help="把候选登记为被某成功轮复用（触发转正判定）")
    promote.add_argument("cid")
    promote.add_argument("--round", dest="round_no", type=int, required=True)
    promote.add_argument("--db", default="kfpa.db")
    promote.add_argument("--json", action="store_true")

    ingest = sub.add_parser("ingest", help="离线入闸一条候选（P3 闸门全量的验收入口）")
    ingest.add_argument("--cid", required=True)
    ingest.add_argument("--round", dest="round_no", type=int, required=True)
    ingest.add_argument("--content", required=True)
    ingest.add_argument("--confidence", default="0")
    ingest.add_argument("--kind", choices=list(KINDS), default="success")
    ingest.add_argument("--db", default="kfpa.db")
    ingest.add_argument("--json", action="store_true")

    sample = sub.add_parser("sample", help="抽检：给 --cid 即留痕；不给则列待抽检队列")
    sample.add_argument("--cid", default=None)
    sample.add_argument("--by", default="")
    sample.add_argument("--fail", "--failed", dest="failed", action="store_true",
                        help="抽检结论：不通过 → dropped + 回避模式")
    sample.add_argument("--pass", "--passed", dest="passed", action="store_true",
                        help="抽检结论：通过（默认）")
    sample.add_argument("--db", default="kfpa.db")
    sample.add_argument("--json", action="store_true")

    verdict = sub.add_parser("verdict", help="对某候选下抽检结论（--pass/--fail，落留痕）")
    verdict.add_argument("cid")
    verdict.add_argument("--pass", "--passed", dest="passed", action="store_true",
                         help="抽检通过")
    verdict.add_argument("--fail", "--failed", dest="failed", action="store_true",
                         help="抽检不通过 → dropped + 回避模式")
    verdict.add_argument("--by", default="")
    verdict.add_argument("--db", default="kfpa.db")
    verdict.add_argument("--json", action="store_true")

    report_p = sub.add_parser("report", help="闸门报告（稳定键 JSON）")
    report_p.add_argument("--out", default="", help="落盘路径（缺省打到 stdout）")
    report_p.add_argument("--db", default="kfpa.db")
    report_p.add_argument("--json", action="store_true")

    def _store(db: str):
        try:
            from .store import make_store

            return make_store(db)
        except (ImportError, AttributeError):
            from .store import Store

            return Store(db)

    def _list(args) -> int:
        store = _store(args.db)
        status = CandidateStatus(args.status) if args.status else None
        items = store.list_candidates(status)
        if args.json:
            print(dumps({"candidates": items, "sample": review_sample(store, items),
                         "pending_sample": pending_sample(store)}))
        else:
            print(format_report(items))
            sample = review_sample(store, items)
            print(f"抽检命中 {len(sample)} 条（D3-3.5 抽样率动态）")
        return 0

    def _promote(args) -> int:
        store = _store(args.db)
        candidate = mark_reused(store, args.cid, args.round_no, passed=True)
        if args.json:
            print(dumps(candidate))
            return 0
        print(f"{candidate.cid}：状态 {candidate.status.value}，成功复用轮次 {candidate.reused_by or '-'}")
        if candidate.kind == "avoid":
            print("  （kind=avoid：负知识只进回灌区，永不转正）")
        elif candidate.status is not CandidateStatus.PROMOTED:
            print(f"  （转正需**后续轮次**复用：本候选第 {candidate.round_no} 轮入库，"
                  f"后续复用轮次 {later_rounds(candidate) or '-'}）")
        return 0

    def _ingest(args) -> int:
        store = _store(args.db)
        candidate = make_candidate(
            args.cid,
            args.round_no,
            args.content,
            kind=args.kind,
            confidence=args.confidence,
        )
        admit(store, candidate)
        if args.json:
            print(dumps(candidate))
            return 0
        print(f"{candidate.cid}：置信度 {candidate.confidence:.2f}（门槛 {confidence_threshold():.2f}）"
              f"→ {candidate.status.value}")
        return 0

    def _sample(args) -> int:
        store = _store(args.db)
        if not args.cid:
            queue = pending_sample(store)
            if args.json:
                print(dumps({"pending_sample": queue, "count": len(queue)}))
            else:
                print(f"待抽检 {len(queue)} 条（按 D3-3.5 动态抽样率）")
                for row in queue:
                    print(f"  {row['cid']}（状态 {row['status']}，抽样率 {row['sample_rate']}）")
            return 0
        sample = record_sample(
            store, args.cid, not args.failed, sampled_by=args.by or _default_user()
        )
        if args.json:
            print(dumps(sample))
            return 0
        print(f"{sample.cid}：抽检 {'通过' if sample.passed else '未通过'}"
              f"（by={sample.sampled_by}，抽样率 {sample.rate}）")
        if not sample.passed:
            print("  → 已 dropped，且内容已写入回避模式清单（负知识闭环）")
        return 0

    def _verdict(args) -> int:
        store = _store(args.db)
        passed = not bool(args.failed)
        if getattr(args, "passed", False):
            passed = True
        sample = record_sample(store, args.cid, passed, sampled_by=args.by or _default_user())
        if args.json:
            print(dumps(sample))
            return 0
        print(f"{sample.cid}：抽检结论 {'passed' if sample.passed else 'failed'}"
              f"（by={sample.sampled_by}，抽样率 {sample.rate}）")
        return 0

    def _report(args) -> int:
        store = _store(args.db)
        payload = report(store)
        text = dumps(payload)
        if args.out:
            from pathlib import Path

            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text + "\n", encoding="utf-8")
            print(f"闸门报告已落盘：{target}")
        else:
            print(text)
        return 0

    for parser, handler in (
        (listing, _list),
        (promote, _promote),
        (ingest, _ingest),
        (sample, _sample),
        (verdict, _verdict),
        (report_p, _report),
    ):
        parser.set_defaults(handler=handler, func=handler)


def _default_user() -> str:
    for key in ("KFPA_USER", "USER", "USERNAME", "LOGNAME"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return "unknown"


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "SAMPLE_RATE_MAX",
    "SAMPLE_RATE_MIN",
    "SAMPLE_RATE_STEP",
    "KINDS",
    "THRESHOLD_ENV",
    "SAMPLE_FAILED_REASON_KEY",
    "KnowledgeError",
    "confidence_threshold",
    "sampling_rate",
    "make_candidate",
    "admit",
    "can_promote",
    "promotable",
    "later_rounds",
    "mark_reused",
    "reuse_from_package",
    "is_sampled",
    "review_sample",
    "list_samples",
    "pending_sample",
    "record_sample",
    "sample_candidate",
    "report",
    "format_report",
    "register",
]

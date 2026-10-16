"""知識沉淀闸门（P1，D3-3.5 / design.md §5.5 防知识库中毒）。

规则表（照抄 D3-3.5）：

| 阶段 | 规则 |
|---|---|
| 入隔离区 | `confidence >= 0.80` 且内容非空；低于阈值或空内容 → 直接 `dropped` |
| 转正 | 被 ≥1 个**成功轮次**（门禁 PASS）复用 → `promoted` |
| 负知识 | `kind=avoid` 直接进任务包回灌区（feedback），**不进知识库正区**（永不转正） |
| 抽检 | 抽样率 `max(0.05, 0.20 - 0.05 * 成功复用次数)`（封顶 0.20、下限 0.05） |

本模块是纯逻辑 + Store 读写，**不调用模型**；抽样用内容哈希做确定性判定（同输入同输出，
台架可复现），不使用 `random`，否则 A15 会变成概率性断言。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Optional

from .schemas import CandidateStatus, KnowledgeCandidate, TaskPackage

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from .store import Store

#: 入隔离区置信度门槛（D3-3.5）
CONFIDENCE_THRESHOLD = 0.80
#: 抽检率区间
SAMPLE_RATE_MAX = 0.20
SAMPLE_RATE_MIN = 0.05
SAMPLE_RATE_STEP = 0.05

#: 允许的 kind（success 正知识 / avoid 负知识）
KINDS = ("success", "avoid")


class KnowledgeError(RuntimeError):
    """沉淀闸门可判定错误（中文、三段式）。"""


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
    """入隔离区判定（D3-3.5）：不达标直接 dropped，达标进 quarantine 并落库。"""
    if not candidate.content:
        candidate.status = CandidateStatus.DROPPED
    elif candidate.kind == "avoid":
        # 负知识也要留痕（回避模式清单），但不进正区；状态照常 quarantine，永不 promote
        candidate.status = CandidateStatus.QUARANTINE
    elif candidate.confidence < CONFIDENCE_THRESHOLD:
        candidate.status = CandidateStatus.DROPPED
    else:
        candidate.status = CandidateStatus.QUARANTINE
    store.add_candidate(candidate)
    return candidate


def promotable(candidate: KnowledgeCandidate) -> bool:
    """可转正：隔离区 + 至少 1 个成功轮复用；`kind=avoid` 永不转正（D3-3.5）。"""
    return candidate.kind != "avoid" and candidate.can_promote()


def mark_reused(
    store: "Store", cid: str, round_no: int, *, passed: bool
) -> KnowledgeCandidate:
    """标记候选在某轮被复用；**只有 PASS 轮**才计入 `reused_by` 并触发转正。"""
    candidate = store.get_candidate(cid)
    if candidate is None:
        raise KnowledgeError(
            f"[knowledge] 候选不存在：{cid} → 先用 `kfpa knowledge list` 查 cid"
        )
    if passed and int(round_no) not in candidate.reused_by:
        candidate.reused_by.append(int(round_no))
        store.add_candidate(candidate)  # 覆盖式写回
        if promotable(candidate):
            return store.promote_candidate(cid, int(round_no))
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
    """注册 `kfpa knowledge list|promote`（D3-3.5）。"""
    parser = subparsers.add_parser("knowledge", help="知識沉淀闸门（D3-3.5）")
    sub = parser.add_subparsers(dest="knowledge_cmd", required=True)

    listing = sub.add_parser("list", help="列沉淀候选（可按状态过滤）")
    listing.add_argument("--status", choices=[s.value for s in CandidateStatus], default=None)
    listing.add_argument("--db", default="kfpa.db")
    listing.add_argument("--json", action="store_true")

    promote = sub.add_parser("promote", help="把候选登记为被某成功轮复用（触发转正判定）")
    promote.add_argument("cid")
    promote.add_argument("--round", dest="round_no", type=int, required=True)
    promote.add_argument("--db", default="kfpa.db")

    def _store(db: str):
        try:
            from .store import make_store

            return make_store(db)
        except (ImportError, AttributeError):
            from .store import Store

            return Store(db)

    def _list(args) -> int:
        from .schemas import dumps

        store = _store(args.db)
        status = CandidateStatus(args.status) if args.status else None
        items = store.list_candidates(status)
        if args.json:
            print(dumps({"candidates": items, "sample": review_sample(store, items)}))
        else:
            print(format_report(items))
            sample = review_sample(store, items)
            print(f"抽检命中 {len(sample)} 条（D3-3.5 抽样率动态）")
        return 0

    def _promote(args) -> int:
        store = _store(args.db)
        candidate = mark_reused(store, args.cid, args.round_no, passed=True)
        print(f"{candidate.cid}：状态 {candidate.status.value}，成功复用轮次 {candidate.reused_by or '-'}")
        if candidate.kind == "avoid":
            print("  （kind=avoid：负知识只进回灌区，永不转正）")
        return 0

    listing.set_defaults(handler=_list, func=_list)
    promote.set_defaults(handler=_promote, func=_promote)


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "SAMPLE_RATE_MAX",
    "SAMPLE_RATE_MIN",
    "SAMPLE_RATE_STEP",
    "KINDS",
    "KnowledgeError",
    "sampling_rate",
    "make_candidate",
    "admit",
    "promotable",
    "mark_reused",
    "reuse_from_package",
    "is_sampled",
    "review_sample",
    "format_report",
    "register",
]

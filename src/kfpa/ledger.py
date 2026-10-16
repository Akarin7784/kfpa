"""token 账本（P1，D3-3.4）。

- 每次 `LLM.complete()` 必须落一条 `LedgerEntry`，且**只在 wrapper 里实现**（不散落在调用点）：
  用 `wrap_llm(llm, ledger, ...)` 包一层，业务代码继续只认 `COMPLETE(role, system, user)`。
- `local`：只落本地表（离线可验收，A11 的主力）；`langfuse`：额外上报，**失败只警告不崩**。
- `model_ref` 只能来自 `config/models.yaml`（经 `roles` 传入），代码里不许写死模型串（A5）；取不到就空串。
- `estimate_tokens(text) = 字符数 // 3`（确定性）；真实回包带 usage 时**优先级更高**。
- `cost` 只在 roles 配置里给了单价时才计算，否则 `0.0` —— **不许编造**。
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .schemas import LedgerEntry

#: token 估算比率（D3-3.4：字符数 // 3）
CHARS_PER_TOKEN = 3

#: roles 配置里可选的单价字段（元 / 千 token）；没有就记 0.0
PRICE_IN_KEYS = ("price_per_1k_in", "input_price_per_1k", "cost_per_1k_in")
PRICE_OUT_KEYS = ("price_per_1k_out", "output_price_per_1k", "cost_per_1k_out")


@runtime_checkable
class Ledger(Protocol):
    """账本协议（D3-3.4）。"""

    def record(self, entry: LedgerEntry) -> None: ...


def estimate_tokens(text: str | None) -> int:
    """确定性 token 估算：`max(1, len(str) // 3)`；空文本记 0。"""
    body = "" if text is None else str(text)
    if not body:
        return 0
    return max(1, len(body) // CHARS_PER_TOKEN)


def _role_models(roles: Optional[dict[str, dict]]) -> dict[str, str]:
    """`{role: model}`（只取配置里的 model 字段，没有就空串）。"""
    out: dict[str, str] = {}
    for role, conf in (roles or {}).items():
        if isinstance(conf, dict):
            out[str(role)] = str(conf.get("model") or "")
    return out


def _unit_price(roles: Optional[dict[str, dict]], role: str) -> tuple[float, float]:
    conf = (roles or {}).get(role) or {}
    if not isinstance(conf, dict):
        return 0.0, 0.0

    def pick(keys: tuple[str, ...]) -> float:
        for key in keys:
            if key in conf:
                try:
                    return float(conf[key])
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    return pick(PRICE_IN_KEYS), pick(PRICE_OUT_KEYS)


class LocalLedger:
    """只落本地 `ledger` 表（离线可验收路径）。

    P1 的 `p1/store` 未就位时（P0 七表库或桩 Store）降级为**内存账本 + 一次性警告**，
    保证 P0 的 `round` 不会因为缺 `add_ledger_entry()` 而崩；`kfpa ledger` 的输出以库里为准。
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.entries: list[LedgerEntry] = []   # 本地缓存，便于同进程汇总/自测
        self._warned = False

    def record(self, entry: LedgerEntry) -> None:
        if hasattr(self.store, "add_ledger_entry"):
            self.store.add_ledger_entry(entry)
        elif not self._warned:
            self._warned = True
            print(
                "[ledger] store 缺少 add_ledger_entry()（P1 表未就位）→ 本次只记内存，不落库",
                file=sys.stderr,
                flush=True,
            )
        self.entries.append(entry)

    def list(self, round_no: Optional[int] = None) -> list[LedgerEntry]:
        if hasattr(self.store, "list_ledger"):
            return list(self.store.list_ledger(round_no))
        return [e for e in self.entries if round_no is None or e.round_no == round_no]


class LangfuseLedger(LocalLedger):
    """本地表 + Langfuse 上报（可选）；上报失败只警告，不影响流水线（D3-3.4）。"""

    def __init__(self, store: Any, **kwargs: Any) -> None:
        super().__init__(store)
        self._client: Any = None
        self._warned = False
        try:  # 惰性导入：缺包不崩，只降级
            import langfuse  # type: ignore

            self._client = langfuse.Langfuse(**kwargs) if kwargs else langfuse.Langfuse()
        except Exception as exc:  # pragma: no cover - 取决于本机是否装 langfuse
            print(
                f"[ledger] langfuse 不可用（{exc}）→ 降级为仅本地表，A11 不受影响",
                file=sys.stderr,
                flush=True,
            )
            self._client = None

    def record(self, entry: LedgerEntry) -> None:
        super().record(entry)
        if self._client is None:
            if not self._warned:
                self._warned = True
            return
        try:  # pragma: no cover - 真实上报
            self._client.trace(
                name=f"kfpa-round-{entry.round_no}",
                metadata={
                    "role": entry.role,
                    "model_ref": entry.model_ref,
                    "tokens_in": entry.tokens_in,
                    "tokens_out": entry.tokens_out,
                    "source": entry.source,
                },
            )
        except Exception as exc:  # pragma: no cover
            print(f"[ledger] langfuse 上报失败（{exc}）→ 只记本地表", file=sys.stderr, flush=True)


def make_ledger(store: Any, provider: str = "local", **kwargs: Any) -> Ledger:
    """`local`（默认）或 `langfuse`（额外上报，失败降级）。"""
    if provider == "langfuse":
        return LangfuseLedger(store, **kwargs)
    return LocalLedger(store)


class LedgeredLLM:
    """LLM 包装器：每次 complete 落一条 `LedgerEntry`（D3-3.4）。"""

    def __init__(
        self,
        llm: Any,
        ledger: Ledger,
        *,
        round_no: int | Callable[[], int] = 0,
        roles: Optional[dict[str, dict]] = None,
        source: str = "mock",
    ) -> None:
        self.inner = llm
        self.ledger = ledger
        self._round_no = round_no
        self._models = _role_models(roles)
        self._roles = roles or {}
        self.source = source

    @property
    def name(self) -> str:  # 与 LLM 适配器保持一致的展示名
        return getattr(self.inner, "name", type(self.inner).__name__)

    @property
    def round_no(self) -> int:
        value = self._round_no
        try:
            return int(value() if callable(value) else value)
        except Exception:  # pragma: no cover - 回调异常不该拖垮流水线
            return 0

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        output = self.inner.complete(role, system, user, json_mode=json_mode)
        usage = getattr(self.inner, "last_usage", None)
        if isinstance(usage, dict) and usage:
            tokens_in = int(usage.get("input", usage.get("prompt_tokens", 0)) or 0)
            tokens_out = int(usage.get("output", usage.get("completion_tokens", 0)) or 0)
        else:
            tokens_in = estimate_tokens(system) + estimate_tokens(user)
            tokens_out = estimate_tokens(output)
        price_in, price_out = _unit_price(self._roles, role)
        cost = 0.0
        if price_in or price_out:
            cost = round(tokens_in / 1000.0 * price_in + tokens_out / 1000.0 * price_out, 6)
        self.ledger.record(
            LedgerEntry(
                round_no=self.round_no,
                role=str(role),
                model_ref=self._models.get(str(role), ""),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost=cost,
                source=self.source,
            )
        )
        return output


def wrap_llm(
    llm: Any,
    ledger: Ledger,
    *,
    round_no: int | Callable[[], int] = 0,
    roles: Optional[dict[str, dict]] = None,
    source: str = "mock",
) -> LedgeredLLM:
    """把任意 LLM 包成记账版本；已经是 `LedgeredLLM` 的原样返回（避免重复记账）。"""
    if isinstance(llm, LedgeredLLM):
        return llm
    return LedgeredLLM(llm, ledger, round_no=round_no, roles=roles, source=source)


def summarize(entries: list[LedgerEntry]) -> dict:
    """按角色汇总 token 与费用（A11 的断言对象）。"""
    by_role: dict[str, dict] = {}
    for entry in entries:
        bucket = by_role.setdefault(
            str(entry.role), {"tokens_in": 0, "tokens_out": 0, "cost": 0.0, "calls": 0}
        )
        bucket["tokens_in"] += int(entry.tokens_in)
        bucket["tokens_out"] += int(entry.tokens_out)
        bucket["cost"] = round(bucket["cost"] + float(entry.cost), 6)
        bucket["calls"] += 1
    totals = {
        "tokens_in": sum(b["tokens_in"] for b in by_role.values()),
        "tokens_out": sum(b["tokens_out"] for b in by_role.values()),
        "cost": round(sum(b["cost"] for b in by_role.values()), 6),
        "calls": sum(b["calls"] for b in by_role.values()),
    }
    totals["total_tokens"] = totals["tokens_in"] + totals["tokens_out"]
    return {"by_role": by_role, "totals": totals}


def format_report(entries: list[LedgerEntry]) -> str:
    """CLI 文本（每轮每角色 token 与合计，D3-3.4）。"""
    data = summarize(entries)
    if not data["by_role"]:
        return "（账本为空）"
    lines = ["角色        调用  输入token  输出token  合计     费用"]
    for role, bucket in sorted(data["by_role"].items()):
        total = bucket["tokens_in"] + bucket["tokens_out"]
        lines.append(
            f"{role:<11} {bucket['calls']:<5} {bucket['tokens_in']:<9} {bucket['tokens_out']:<9} "
            f"{total:<8} {bucket['cost']}"
        )
    totals = data["totals"]
    lines.append(
        f"{'合计':<11} {totals['calls']:<5} {totals['tokens_in']:<9} {totals['tokens_out']:<9} "
        f"{totals['total_tokens']:<8} {totals['cost']}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI 挂载（供 cli.py 调用）


def register(subparsers) -> None:  # pragma: no cover - 由 cli.py 挂载
    """注册 `kfpa ledger [--round N] [--json]`（D3-3.4）。"""
    parser = subparsers.add_parser("ledger", help="token 账本（D3-3.4）")
    parser.add_argument("--round", dest="round_no", type=int, default=None)
    parser.add_argument("--db", default="kfpa.db")
    parser.add_argument("--json", action="store_true")

    def _run(args) -> int:
        from .schemas import dumps

        try:
            from .store import make_store

            store = make_store(args.db)
        except (ImportError, AttributeError):
            from .store import Store

            store = Store(args.db)
        entries = list(store.list_ledger(args.round_no))
        if args.json:
            print(dumps({"entries": entries, "summary": summarize(entries)}))
        else:
            print(format_report(entries))
        return 0

    parser.set_defaults(handler=_run, func=_run)


__all__ = [
    "CHARS_PER_TOKEN",
    "Ledger",
    "estimate_tokens",
    "LocalLedger",
    "LangfuseLedger",
    "make_ledger",
    "LedgeredLLM",
    "wrap_llm",
    "summarize",
    "format_report",
    "register",
]

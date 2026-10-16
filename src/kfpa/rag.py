"""检索接入（D5 §3.5）：**本地回退检索**（离线验收路径）+ Dify 惰性适配器（接线即交付）。

三条硬口径（对应 A27）：

1. **本地回退可离线命中且稳定排序**：对 `knowledge` 表（`PROMOTED` 优先）+ `docs/**` 做
   朴素关键词召回，排序是**全序**（`PROMOTED` → 分数 → 来源路径），同输入同输出；
2. **未配 Dify 凭据 → rc=4 + 中文**（不是 5，也不是裸崩）；真实 HTTP 只在配了凭据时发生；
3. **降级可见**：回退命中标 `provider="local"`，绝不把回退结果伪装成 Dify 结果；
   任何日志、异常、落库内容里**不出现 key 明文**（`mask_secret` / `mask_url`）。

无第三方依赖：HTTP 用标准库 `urllib`，所以「未装依赖」这一档在本模块不成立，
只有「未配凭据」（rc=4）与「网络/协议失败」（rc=5）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .schemas import CandidateStatus, dumps

#: 环境变量（D5 §3.5）
DIFY_URL_ENV = "DIFY_API_URL"
DIFY_API_KEY_ENV = "DIFY_API_KEY"

PROVIDER_LOCAL = "local"
PROVIDER_DIFY = "dify"
PROVIDER_AUTO = "auto"

#: 默认命中条数
DEFAULT_TOP_K = 5
#: 检索哪些文档后缀
DOC_SUFFIXES = (".md", ".txt")
#: 单文件读取上限（防止误读大文件拖慢一轮）
DOC_MAX_BYTES = 400_000

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


class RagError(RuntimeError):
    """检索可判定错误（中文、三段式）；CLI 记 rc=5。"""


class RagDependencyError(RagError):
    """凭据/依赖缺失（D5 §3.5：rc=4，不许裸崩，也不许漏 key）。"""


@dataclass
class RagHit:
    """一条命中：`kind` 区分知识库与文档，`summary` 是进任务包的稳定摘要串。"""

    key: str                  # 稳定标识：知识 `K1` / 文档相对路径
    kind: str                 # knowledge | docs
    score: float
    summary: str              # 进 TaskPackage.knowledge_hits 的稳定串
    source: str = ""          # 展示用来源
    promoted: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "score": self.score,
            "summary": self.summary,
            "source": self.source,
            "promoted": self.promoted,
        }


@dataclass
class SearchResult:
    """检索结果（含降级可见性）。"""

    query: str
    provider: str = PROVIDER_LOCAL
    hits: list[RagHit] = field(default_factory=list)
    fallback: bool = False
    note: str = ""

    def summaries(self) -> list[str]:
        return [h.summary for h in self.hits]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "fallback": self.fallback,
            "note": self.note,
            "hits": [h.to_jsonable() for h in self.hits],
        }


# ---------------------------------------------------------------- 打码（A27：不许漏 key）


def mask_secret(value: Optional[str]) -> str:
    """密钥打码：只回长度，连前后缀都不给（日志里出现即算泄漏）。"""
    text = str(value or "")
    if not text:
        return "（未配置）"
    return f"***（{len(text)} 字符，已打码）"


def mask_url(url: Optional[str]) -> str:
    """URL 打码：保留 scheme/host/path，query 与 userinfo 一律抹掉。"""
    text = str(url or "").strip()
    if not text:
        return "（未配置）"
    base = text.split("?", 1)[0].split("#", 1)[0]
    if "://" in base:
        scheme, rest = base.split("://", 1)
        if "@" in rest.split("/", 1)[0]:
            host = rest.split("/", 1)[0].split("@", 1)[1]
            tail = rest[len(rest.split("/", 1)[0]):]
            base = f"{scheme}://{host}{tail}"
    return base + ("?（query 已打码）" if ("?" in text or "#" in text) else "")


# ---------------------------------------------------------------- 关键词召回（本地）


def keywords(text: str) -> list[str]:
    """切词：ASCII 词 + 中文二元组（中文串 >2 字时取相邻二元组，避免单字噪声）。"""
    out: list[str] = []
    for token in _TOKEN.findall(str(text or "").lower()):
        if _CJK.fullmatch(token):
            if len(token) <= 2:
                out.append(token)
            else:
                out.extend(token[i : i + 2] for i in range(len(token) - 1))
        else:
            out.append(token)
    seen: list[str] = []
    for item in out:
        if item and item not in seen:
            seen.append(item)
    return seen


def _score(text: str, terms: list[str]) -> float:
    """命中分：每个词的出现次数求和（单词封顶 3，防长文靠重复堆分）。"""
    low = str(text or "").lower()
    total = 0.0
    for term in terms:
        if not term:
            continue
        total += min(low.count(term), 3)
    return float(total)


def _promoted_rank(hit: RagHit) -> int:
    return 0 if hit.promoted else 1


def _sort_key(hit: RagHit) -> tuple[int, float, str, str]:
    """全序：`PROMOTED` 优先 → 分数降序 → 来源 → key（同输入同输出）。"""
    return (_promoted_rank(hit), -hit.score, hit.source, hit.key)


def _docs_root(docs_dir: Optional[str] = None) -> Path:
    if docs_dir:
        return Path(docs_dir)
    return Path(__file__).resolve().parents[2] / "docs"


def _knowledge_hits(store: Any, terms: list[str]) -> list[RagHit]:
    list_candidates = getattr(store, "list_candidates", None)
    if not callable(list_candidates):
        return []
    try:
        candidates = list(list_candidates())
    except Exception:  # pragma: no cover - 桩缺实现
        return []
    hits: list[RagHit] = []
    for candidate in candidates:
        text = f"{getattr(candidate, 'content', '')}"
        score = _score(text, terms)
        if score <= 0:
            continue
        promoted = getattr(candidate, "status", None) is CandidateStatus.PROMOTED
        reused = len(getattr(candidate, "reused_by", []) or [])
        hits.append(
            RagHit(
                key=str(getattr(candidate, "cid", "")),
                kind="knowledge",
                score=score,
                summary=f"{candidate.cid}#reused={reused}",
                source=str(getattr(candidate, "content", ""))[:60],
                promoted=promoted,
            )
        )
    return hits


def _docs_hits(terms: list[str], docs_dir: Optional[str] = None, root: Optional[Path] = None) -> list[RagHit]:
    base = root or _docs_root(docs_dir)
    if not base.is_dir():
        return []
    hits: list[RagHit] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DOC_SUFFIXES:
            continue
        try:
            if path.stat().st_size > DOC_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matched = sum(1 for term in terms if term and term in text.lower())
        score = _score(text, terms)
        if score <= 0:
            continue
        rel = path.relative_to(base).as_posix()
        hits.append(
            RagHit(
                key=rel,
                kind="docs",
                score=score,
                summary=f"docs/{rel}#hits={matched}",
                source=f"docs/{rel}",
                promoted=False,
            )
        )
    return hits


def local_search(
    query: str,
    *,
    store: Any = None,
    docs_dir: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    docs_root: Optional[Path] = None,
) -> SearchResult:
    """本地回退检索（验收路径，离线、确定、稳定排序）。"""
    terms = keywords(query)
    result = SearchResult(query=str(query), provider=PROVIDER_LOCAL, fallback=False)
    if not terms:
        result.note = "查询为空（或全是分隔符）→ 不编造命中"
        return result
    hits = _knowledge_hits(store, terms) if store is not None else []
    hits.extend(_docs_hits(terms, docs_dir, docs_root))
    hits.sort(key=_sort_key)
    limit = max(1, int(top_k))
    result.hits = hits[:limit]
    result.note = f"本地回退检索：命中 {len(result.hits)} 条（候选池 {len(hits)} 条）"
    return result


# ---------------------------------------------------------------- Dify 适配器（接线即交付）


def dify_credentials(
    url: Optional[str] = None, api_key: Optional[str] = None
) -> tuple[str, str]:
    """取 Dify 凭据；缺失 → `RagDependencyError`（rc=4），消息里**只有打码信息**。"""
    resolved_url = str(url or os.environ.get(DIFY_URL_ENV) or "").strip()
    resolved_key = str(api_key or os.environ.get(DIFY_API_KEY_ENV) or "").strip()
    missing = []
    if not resolved_url:
        missing.append(DIFY_URL_ENV)
    if not resolved_key:
        missing.append(DIFY_API_KEY_ENV)
    if missing:
        raise RagDependencyError(
            "[rag] Dify 凭据缺失：" + "、".join(missing) +
            f"（{DIFY_URL_ENV}={mask_url(resolved_url)}，{DIFY_API_KEY_ENV}={mask_secret(resolved_key)}）"
            " → 配置这两个环境变量，或改用 `--provider local`（本地回退检索，离线可用）；退出码 4"
        )
    return resolved_url, resolved_key


def dify_search(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 10.0,
) -> SearchResult:
    """调用 Dify 检索接口（只在配了凭据时发生真实 HTTP）。

    - 未配凭据 → `RagDependencyError`（rc=4）；
    - 网络/协议失败 → `RagError`（rc=5），消息里 URL 与 key 都打码；
    - 返回体按 Dify `records[*].segment/score` 尽力解析，解析不出的字段留空（不编造）。
    """
    resolved_url, resolved_key = dify_credentials(url, api_key)
    payload = json.dumps(
        {"query": str(query), "top_k": max(1, int(top_k)), "score_threshold": 0.0},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        resolved_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {resolved_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RagError(
            f"[rag] Dify 检索失败：HTTP {exc.code}（{mask_url(resolved_url)}，"
            f"key={mask_secret(resolved_key)}）→ 检查地址/数据集权限；退出码 5"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RagError(
            f"[rag] Dify 检索不可达：{exc}（{mask_url(resolved_url)}，key={mask_secret(resolved_key)}）"
            " → 检查网络与 DIFY_API_URL；退出码 5"
        ) from exc
    try:
        parsed = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise RagError(
            f"[rag] Dify 返回体不是 JSON（{mask_url(resolved_url)}）→ 确认 DIFY_API_URL 指向检索接口；退出码 5"
        ) from exc

    hits: list[RagHit] = []
    records = parsed.get("records") if isinstance(parsed, dict) else None
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict):
            continue
        segment = item.get("segment") if isinstance(item.get("segment"), dict) else {}
        key = str(segment.get("id") or item.get("id") or "").strip() or "dify:?"
        content = str(segment.get("content") or item.get("content") or "").strip()
        score = float(item.get("score") or 0.0)
        hits.append(
            RagHit(
                key=key,
                kind="dify",
                score=score,
                summary=f"dify:{key}#score={score:.3f}",
                source=content[:60],
                promoted=False,
            )
        )
    hits.sort(key=_sort_key)
    return SearchResult(
        query=str(query),
        provider=PROVIDER_DIFY,
        hits=hits[: max(1, int(top_k))],
        fallback=False,
        note=f"Dify 检索命中 {len(hits)} 条（{mask_url(resolved_url)}）",
    )


def search(
    query: str,
    *,
    provider: str = PROVIDER_AUTO,
    store: Any = None,
    docs_dir: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    docs_root: Optional[Path] = None,
) -> SearchResult:
    """统一入口：`auto`（有凭据走 Dify，否则回退且标 `provider=local`）/ `local` / `dify`。

    显式 `provider="dify"` 而没凭据 → **rc=4**（不静默回退，否则「Dify 结果」会撒谎）。
    """
    resolved = str(provider or PROVIDER_AUTO).strip().lower()
    if resolved == PROVIDER_LOCAL:
        return local_search(query, store=store, docs_dir=docs_dir, top_k=top_k, docs_root=docs_root)
    if resolved not in (PROVIDER_AUTO, PROVIDER_DIFY):
        raise RagError(
            f"[rag] 未知 provider：{provider} → 取 auto / local / dify 之一；退出码 2"
        )
    if resolved == PROVIDER_DIFY:
        return dify_search(query, top_k=top_k, url=url, api_key=api_key)
    has_creds = bool(str(url or os.environ.get(DIFY_URL_ENV) or "").strip()) and bool(
        str(api_key or os.environ.get(DIFY_API_KEY_ENV) or "").strip()
    )
    if not has_creds:
        result = local_search(query, store=store, docs_dir=docs_dir, top_k=top_k, docs_root=docs_root)
        result.fallback = True
        result.note = (
            "Dify 未配置（" + DIFY_URL_ENV + " / " + DIFY_API_KEY_ENV + "）→ 本地回退检索"
            f"（provider={PROVIDER_LOCAL}）；"
            + result.note
        )
        return result
    return dify_search(query, top_k=top_k, url=url, api_key=api_key)


def hits_for_package(
    package: Any, *, store: Any = None, top_k: int = 3, docs_dir: Optional[str] = None
) -> list[str]:
    """任务包的检索命中摘要（进 `TaskPackage.knowledge_hits`）。

    查询由任务包自造（feature + goal，**不含随机/时间**），保证同包同命中；
    全程 `provider=local`（离线验收路径），任何异常都退回空清单，不让检索拖垮一轮。
    """
    feature = str(getattr(package, "feature", "") or "").strip()
    goal = " ".join(str(getattr(package, "goal", "") or "").split())
    query = (feature + " " + goal).strip()
    if not query:
        return []
    try:
        result = local_search(query, store=store, docs_dir=docs_dir, top_k=top_k)
    except Exception:  # noqa: BLE001 - 检索失败只降级，不影响门禁
        return []
    return result.summaries()


# ---------------------------------------------------------------- CLI


def _store_for(db: str) -> Any:
    try:
        from .store import make_store

        return make_store(db)
    except (ImportError, AttributeError):  # pragma: no cover - 老环境兜底
        from .store import Store

        return Store(db)


def register(subparsers) -> None:  # pragma: no cover - 由 cli.py 挂载
    """注册 `kfpa rag search "查询" [--provider auto|local|dify] [--top-k N] [--json]`。"""
    parser = subparsers.add_parser("rag", help="检索接入（本地回退 + Dify 惰性适配器，D5 §3.5）")
    sub = parser.add_subparsers(dest="rag_cmd", required=True)

    search_p = sub.add_parser("search", help="检索：auto（默认）/ local（离线回退）/ dify")
    search_p.add_argument("query")
    search_p.add_argument("--provider", choices=[PROVIDER_AUTO, PROVIDER_LOCAL, PROVIDER_DIFY],
                          default=PROVIDER_AUTO)
    search_p.add_argument("--top-k", dest="top_k", type=int, default=DEFAULT_TOP_K)
    search_p.add_argument("--db", default="kfpa.db")
    search_p.add_argument("--docs-dir", dest="docs_dir", default=None)
    search_p.add_argument("--json", action="store_true")

    def _run(args) -> int:
        try:
            store = _store_for(args.db) if args.provider != PROVIDER_DIFY else None
        except Exception:  # noqa: BLE001 - 库不可用不该挡住检索（Dify 路径压根不用库）
            store = None
        try:
            result = search(
                args.query,
                provider=args.provider,
                store=store,
                docs_dir=getattr(args, "docs_dir", None),
                top_k=args.top_k,
            )
        except RagDependencyError as exc:
            print(str(exc), file=_stderr())
            return 4
        except RagError as exc:
            print(str(exc), file=_stderr())
            return 5
        if args.json:
            print(dumps(result.to_jsonable()))
            return 0
        print(f"provider={result.provider}｜fallback={result.fallback}｜命中 {len(result.hits)} 条")
        if result.note:
            print(f"  {result.note}")
        for hit in result.hits:
            print(f"  [{hit.kind}] {hit.summary}（score={hit.score:g}）")
        return 0

    search_p.set_defaults(handler=_run, func=_run)


def _stderr():
    import sys

    return sys.stderr


__all__ = [
    "DIFY_URL_ENV",
    "DIFY_API_KEY_ENV",
    "PROVIDER_LOCAL",
    "PROVIDER_DIFY",
    "PROVIDER_AUTO",
    "DEFAULT_TOP_K",
    "RagError",
    "RagDependencyError",
    "RagHit",
    "SearchResult",
    "mask_secret",
    "mask_url",
    "keywords",
    "local_search",
    "dify_credentials",
    "dify_search",
    "search",
    "hits_for_package",
    "register",
]

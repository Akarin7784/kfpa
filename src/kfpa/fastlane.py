"""快车道**落库与攒批承认**（D5 §3.2，兑现 R-23 的「P3 才接攒批承认入库」）。

分工：判据纯函数 + 状态机 + CLI 在本模块（长流）；`fastlane_requests` 表的
SQLite/PG 双驱动在 `store/**`（磐石）。本模块**不调用模型**，全部可离线跑。

三条硬口径（D5 §3.2，逐条对应验收 A23/A24）：

1. **判据只做纯计算**：`judge()` 就是 `FastlaneRequest.judged_fastlane()`——
   不新增/不修改接口契约 · 不改数据模型 · 波及面 ≤ 1 个模块；任一为假走 `ESCALATED`
   且 `escalations()` 指名是哪一条（不许只给布尔）。
2. **幂等与留痕**：同一 `request_id` 二次承认返回原记录、**不覆盖**首次
   `decided_by/decided_at/batch_id`；同批 `batch-ack` 共享同一个 `batch_id`；
   `ESCALATED`/`REJECTED` **不许**被 `ack` 翻成 `APPROVED`（要翻必须新开请求，rc=3）。
3. **`no_diff_source` 不猜**（R-23.2 第三来源）：拿不到改动清单时既不判 True 也不判
   False → 登记为 `ESCALATED` 且 `note="no_diff_source"` 显式可见，绝不冒充快车道。

退出码沿用 D1-5/R-31 三分：`0` 成功 · `2` 用法/参数错 · `3` 需人工介入（升级回完整设计
回合）· `4` 依赖缺失 · `5` I/O 与数据库。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .schemas import FastlaneRequest, FastlaneStatus, dumps

#: 退出码（D1-5 + R-31 三分）
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MANUAL = 3          # 需人工介入：状态冲突 / 升级回完整设计回合
EXIT_DEP_MISSING = 4
EXIT_IO = 5

#: 第三来源（R-23.2）：拿不到改动清单
NO_DIFF_SOURCE = "no_diff_source"

#: 微型规格模板（D5 §2：`src/kfpa/prompts/fastlane.j2`）
MICRO_SPEC_TEMPLATE = "fastlane.j2"


class FastlaneError(RuntimeError):
    """快车道可判定错误（中文、三段式；rc=5 由 CLI 兜底）。"""


class FastlaneConflict(FastlaneError):
    """状态冲突：ESCALATED/REJECTED 不许被翻白（rc=3，需人工）。"""


# ---------------------------------------------------------------- 判据（纯函数）


def judge(request: FastlaneRequest) -> bool:
    """三判据全真才算快车道（D5 §3.2）；**拿不到改动清单（`diff_files` 为空）→ False**。

    R-23.2 的第三来源是「不猜」：没有改动清单就**无法证明**「波及面 ≤ 1 个模块」，
    这里既不许按 True 放行，也不许当「三判据全真」——A24 明确断这一条。
    """
    if not request.diff_files:
        return False
    return bool(request.judged_fastlane())


def escalate_reasons(request: FastlaneRequest) -> list[str]:
    """不满足判据的原因清单；`no_diff_source` 优先报出来（R-23.2 不猜）。"""
    if str(request.note or "").strip() == NO_DIFF_SOURCE or not request.diff_files:
        return [NO_DIFF_SOURCE]
    return list(request.escalations())


def _contract_files(package: Any) -> list[str]:
    """判据①命中的契约增量行（复用 `impact` 的口径，不另立一套）。"""
    from . import impact

    try:
        return list(impact.contract_delta_lines(package))
    except Exception:  # noqa: BLE001 - 老包缺字段时不编造
        return []


def _is_data_model(files: list[str]) -> bool:
    from . import impact

    return any(impact.is_data_model_touch(f) for f in files)


def _modules(files: list[str]) -> list[str]:
    from . import impact

    return sorted({impact.top_module(f) for f in files if str(f).strip()})


def resolve_diff_files(
    round_no: int, files: Optional[list[str]] = None, workdir: Optional[str] = None
) -> Optional[list[str]]:
    """R-23.2 三来源：① 执行器产出 → ② 显式 `--files` → ③ `None`（不猜）。"""
    from . import impact

    found = impact.files_from_workdir(int(round_no), workdir)
    if found:
        return found
    if files:
        out = [str(f).strip().replace("\\", "/").lstrip("./") for f in files if str(f).strip()]
        if out:
            return out
    return None


def make_request(
    round_no: int,
    seq: int,
    summary: str,
    *,
    package: Any = None,
    files: Optional[list[str]] = None,
    workdir: Optional[str] = None,
) -> FastlaneRequest:
    """按三判据登记一条快车道请求（不落库；落库由调用方 `_save`）。

    `files=None`（拿不到改动清单）→ `note="no_diff_source"` 且判据保持全假，
    由调用方映射成 `ESCALATED`——**绝不用默认 False 冒充「三判据全真」**。
    """
    resolved = resolve_diff_files(round_no, files, workdir)
    deltas = _contract_files(package) if package is not None else []
    new_interface = bool(deltas)
    if resolved is None:
        request = FastlaneRequest(
            request_id=f"fl-{int(round_no)}-{int(seq)}",
            round_no=int(round_no),
            summary=str(summary or "").strip(),
            diff_files=[],
            new_interface=new_interface,
            new_data_model=False,
            touched_modules=[],
            status=FastlaneStatus.ESCALATED,
            note=NO_DIFF_SOURCE,
        )
        return request

    request = FastlaneRequest(
        request_id=f"fl-{int(round_no)}-{int(seq)}",
        round_no=int(round_no),
        summary=str(summary or "").strip(),
        diff_files=list(resolved),
        new_interface=new_interface,
        new_data_model=_is_data_model(list(resolved)),
        touched_modules=_modules(list(resolved)),
        status=FastlaneStatus.PENDING,
        note="",
    )
    if not judge(request):
        request.status = FastlaneStatus.ESCALATED
        request.note = "judgement_failed:" + ",".join(request.escalations())
    return request


def micro_spec(request: FastlaneRequest, package: Any = None) -> str:
    """渲染微型规格（`prompts/fastlane.j2`）；模板缺失时给一段等价纯文本兜底。"""
    from .pipeline import _render  # 懒 import：避免与 pipeline 的循环依赖

    try:
        return _render(
            MICRO_SPEC_TEMPLATE,
            request=request,
            package=package,
            reasons=escalate_reasons(request),
        )
    except Exception:  # noqa: BLE001 - 模板缺失/老环境不让快车道登记能力失效
        head = f"微型规格 {request.request_id}｜第 {request.round_no} 轮｜{request.summary}"
        return head + "\n- 改动文件：" + ("、".join(request.diff_files) or "（无来源）")


# ---------------------------------------------------------------- Store 访问（缺方法降级）


def _call(store: Any, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """调 store 方法；方法缺失/自身报错 → 返回 default（P2 老规矩：只记日志不崩）。"""
    fn = getattr(store, name, None)
    if not callable(fn):
        return default
    return fn(*args, **kwargs)


def save_request(store: Any, request: FastlaneRequest) -> FastlaneRequest:
    """落库一条登记（store 缺方法时返回原对象，由 CLI 在 stderr 记一笔）。"""
    saved = _call(store, "add_fastlane_request", request, default=None)
    return saved if isinstance(saved, FastlaneRequest) else request


def get_request(store: Any, request_id: str) -> Optional[FastlaneRequest]:
    got = _call(store, "get_fastlane_request", str(request_id), default=None)
    return got if isinstance(got, FastlaneRequest) else None


def list_requests(store: Any, status: Optional[str] = None) -> list[FastlaneRequest]:
    items = _call(store, "list_fastlane_requests", status, default=[]) or []
    out = [x for x in items if isinstance(x, FastlaneRequest)]
    return sorted(out, key=lambda r: (int(r.round_no), str(r.request_id)))


def update_request(store: Any, request: FastlaneRequest) -> FastlaneRequest:
    got = _call(store, "update_fastlane_request", request, default=None)
    return got if isinstance(got, FastlaneRequest) else request


def next_seq(store: Any, round_no: int) -> int:
    """同轮的登记序号：`fl-<round>-<seq>`，从 1 起（可复算，不用 uuid）。"""
    same = [r for r in list_requests(store) if int(r.round_no) == int(round_no)]
    return len(same) + 1


def next_batch_id(store: Any, round_no: int, *, prefix: str = "flb") -> str:
    """同轮的下一个批次号：`flb-<round>-<n>`（同批共享，可复算）。"""
    known = {
        str(r.batch_id)
        for r in list_requests(store)
        if int(r.round_no) == int(round_no) and str(r.batch_id or "").strip()
    }
    return f"{prefix}-{int(round_no)}-{len(known) + 1}"


# ---------------------------------------------------------------- 攒批承认状态机


def _persist_decision(
    store: Any,
    request: FastlaneRequest,
    *,
    status: FastlaneStatus,
    by: str,
    batch_id: str = "",
    note: str = "",
    now: Optional[float] = None,
) -> FastlaneRequest:
    """把一次人工决定写进存储（磐石 `p3/store` 的冻结方法优先）。

    - 优先 `store.ack_fastlane_request(id, by=…, status=…, batch_id=…, note=…)`：**幂等与
      「不可翻案」在存储层实现**（二次承认原样返回首次记录，`ESCALATED/REJECTED` 翻不成
      `APPROVED`），本模块的状态检查只是更早的中文反馈。
    - 老库/桩没有该方法时退回「本地改写 + `update_fastlane_request`」（P2 期桩兼容）。
    - `now` 只在显式传入（可复现的台架）时透传；真实 store 不接受就自动去掉。
    """
    decided_by = str(by or "").strip() or "unknown"
    decided_at = float(now if now is not None else time.time())
    writer = getattr(store, "ack_fastlane_request", None)
    if callable(writer):
        payload = {"by": decided_by, "status": status, "batch_id": batch_id, "note": note}
        result: Any = None
        for attempt in ([{**payload, "now": decided_at}, payload] if now is not None else [payload]):
            try:
                result = writer(request.request_id, **attempt)
                break
            except TypeError:  # 真实 store 的签名没有 now
                continue
            except Exception:  # noqa: BLE001 - 存储层自己的错误（KeyError 等）交给调用方
                result = None
                break
        if isinstance(result, FastlaneRequest):
            return result
        stored = get_request(store, request.request_id)
        if stored is not None:
            return stored
    request.status = status
    request.decided_by = decided_by
    request.decided_at = decided_at
    if batch_id:
        request.batch_id = str(batch_id).strip()
    if note:
        request.note = str(note).strip()
    return update_request(store, request)


def ack(
    store: Any,
    request_id: str,
    by: str = "",
    *,
    batch_id: str = "",
    now: Optional[float] = None,
) -> FastlaneRequest:
    """承认一条请求：`PENDING → APPROVED`（**R-33 冻结签名** `ack(store, request_id, by)`）。

    - 已是 `APPROVED` → **幂等**返回原记录（不覆盖首次 `decided_by/decided_at/batch_id`）；
    - `ESCALATED`/`REJECTED` → 抛 `FastlaneConflict`（rc=3，要翻必须新开请求）；
    - 查无此单 → `FastlaneError`（CLI 判 rc=2）。
    """
    request = get_request(store, request_id)
    if request is None:
        raise FastlaneError(
            f"[fastlane] 查无此单：{request_id} → 先 `kfpa fastlane plan --round N` 登记，"
            "再 `kfpa fastlane list` 核对 request_id"
        )
    if request.status is FastlaneStatus.APPROVED:
        return request  # 幂等：首次留痕优先
    if request.status in (FastlaneStatus.ESCALATED, FastlaneStatus.REJECTED):
        raise FastlaneConflict(
            f"[fastlane] {request_id} 当前状态 {request.status.value}，不许翻成 approved"
            f"（原因：{'、'.join(escalate_reasons(request)) or '人工拒绝'}）"
            " → 要再走快车道必须新开一条请求（R-23/D5 §3.2）"
        )
    return _persist_decision(
        store,
        request,
        status=FastlaneStatus.APPROVED,
        by=by,
        batch_id=batch_id or next_batch_id(store, request.round_no, prefix=f"flb-{request_id}"),
        now=now,
    )


def reject(
    store: Any, request_id: str, *, by: str, note: str = "", now: Optional[float] = None
) -> FastlaneRequest:
    """人工拒绝（`PENDING → REJECTED`）；已 APPROVED 的不许被拒（需新开请求）。"""
    request = get_request(store, request_id)
    if request is None:
        raise FastlaneError(f"[fastlane] 查无此单：{request_id} → 先 `kfpa fastlane list`")
    if request.status is FastlaneStatus.APPROVED:
        raise FastlaneConflict(
            f"[fastlane] {request_id} 已 approved（batch={request.batch_id}），"
            "拒绝必须新开一条请求，不许事后翻案"
        )
    if request.status is FastlaneStatus.REJECTED and request.decided_by:
        return request  # 幂等
    return _persist_decision(
        store, request, status=FastlaneStatus.REJECTED, by=by, note=note, now=now
    )


_LIST_KEYS = ("acks", "requests", "request_ids", "ids")
_MISSING = object()
#: R-34 冻结形状（坏形状 → rc=2 的提示统一带这段，避免只给「解析失败」）
_SHAPE_HINT = ("形状：`acks: [{request_id, note?}]`（R-34 冻结）——"
               "`acks:` 必须是列表、元素必须含 `request_id`；"
               "也接受 `acks:` 下的标量列表、`acks: [fl-1-1]` 内联列表、或逐行 request_id 清单")


def _ids_from_list(items: list) -> tuple[list[str], bool]:
    """列表元素 → ids（保序去重）；返回 `(ids, ok)`，`ok=False` 表示有元素缺 `request_id`。"""
    out: list[str] = []
    ok = True
    for item in items:
        if isinstance(item, dict):
            rid = item.get("request_id") or item.get("id")
            value = str(rid).strip() if rid else ""
        else:
            value = str(item).strip() if item is not None else ""
        if not value:
            ok = False          # R-34：元素缺 request_id 不是「跳过」，是坏形状
            continue
        if value not in out:
            out.append(value)
    return out, ok


def _bracket_ids(value: str) -> list[str]:
    """内联列表 `[fl-1-1, fl-1-2]` → ids（保序去重）。"""
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    out: list[str] = []
    for part in inner.split(","):
        item = part.strip().strip("'\"")
        if item and item not in out:
            out.append(item)
    return out


def _parse_acks(text: str, *, strict: bool) -> list[str]:
    """承认清单解析（R-34）：`strict=True` 时任何形状违规都抛 `FastlaneError`（CLI → rc=2）。"""
    raw = str(text or "").strip()
    out: list[str] = []

    def fail(message: str) -> list[str]:
        if strict:
            raise FastlaneError(f"{message} → {_SHAPE_HINT}；rc=2")
        return out

    def from_sequence(items: Any, where: str) -> list[str]:
        ids, ok = _ids_from_list(list(items))
        if not ok:
            return fail(f"[fastlane] {where} 里有元素缺 `request_id`")
        if not ids:
            return fail(f"[fastlane] {where} 是空列表")
        return ids

    if not raw:
        return fail("[fastlane] 承认清单是空文件")
    # 1) JSON
    if raw[0] in "[{":
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = _MISSING
        if payload is not _MISSING:
            if isinstance(payload, list):
                return from_sequence(payload, "JSON 数组")
            if isinstance(payload, dict):
                for key in _LIST_KEYS:
                    if key in payload:
                        if not isinstance(payload[key], list):
                            return fail(
                                f"[fastlane] `{key}` 必须是列表（收到 "
                                f"{type(payload[key]).__name__}：`{payload[key]}`）"
                            )
                        return from_sequence(payload[key], f"`{key}`")
                rid = payload.get("request_id") or payload.get("id")
                return [str(rid).strip()] if rid else fail(
                    "[fastlane] JSON 对象里没有 `request_id`"
                )
            return fail("[fastlane] JSON 顶层必须是数组或对象")
    # 2) YAML（冻结形状的载体；没装 pyyaml 走逐行兜底）
    try:
        import yaml  # type: ignore[import-untyped]

        loaded: Any = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001 - ImportError / YAMLError 都退回逐行解析
        loaded = _MISSING
    if loaded is not _MISSING and (isinstance(loaded, dict) or (isinstance(loaded, list) and loaded)):
        if isinstance(loaded, list):
            return from_sequence(loaded, "YAML 数组")
        for key in _LIST_KEYS:
            if key in loaded:
                if not isinstance(loaded[key], list):
                    return fail(
                        f"[fastlane] `{key}` 必须是列表（收到 "
                        f"{type(loaded[key]).__name__}：`{loaded[key]}`）"
                    )
                return from_sequence(loaded[key], f"`{key}`")
        rid = loaded.get("request_id") or loaded.get("id")
        return [str(rid).strip()] if rid else fail("[fastlane] YAML 对象里没有 `request_id`")
    # 3) 逐行兜底（`- fl-1-1` / `fl-1-1` / `request_id: fl-1-1`）
    for line in raw.splitlines():
        text_line = line.split("#", 1)[0].strip()
        if not text_line:
            continue
        if text_line.endswith(":") and " " not in text_line.rstrip(":"):
            continue  # 块头（`acks:`）不是条目本身
        if text_line.startswith("-"):
            text_line = text_line[1:].strip()
        if ":" in text_line:
            key, _, value = text_line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key in ("request_id", "id"):
                text_line = value
            elif key in _LIST_KEYS:
                if not value.startswith("["):  # `acks: fl-1-1` 是坏形状，不是一条 id
                    return fail(f"[fastlane] `{key}:` 后面必须是列表（收到 `{value}`）")
                for item in _bracket_ids(value):
                    if item not in out:
                        out.append(item)
                continue
            else:
                continue  # `note:` 等其它键忽略
        if text_line and text_line not in out:
            out.append(text_line)
    if not out:
        return fail("[fastlane] 承认清单里没有可识别的 `request_id`")
    return out


def parse_ack_file(text: str) -> list[str]:
    """宽松解析（兼容旧调用）：形状非法时给空清单，不抛异常。

    冻结形状与坏形状口径见 `_parse_acks`；CLI 一律走 `load_ack_ids()`（严格 → rc=2）。
    """
    return _parse_acks(text, strict=False)


def load_ack_ids(text: str) -> list[str]:
    """严格解析 `batch-ack --file`（R-34）：坏形状 → `FastlaneError`（CLI 判 **rc=2**）。

    覆盖三种坏形状 + 空文件 / 空列表 / 无可识别条目：缺 `acks` 键 · 元素缺 `request_id` ·
    `acks` 不是列表（含界面报的 `acks: fl-1-1` 扁平标量）。**不许静默跳过、不许补行。**
    """
    return _parse_acks(text, strict=True)


class BatchAckResult(list):
    """`batch_ack()` 的返回值：**是 list**（R-33 冻结签名 `-> list[FastlaneRequest]`）。

    同时带上批次留痕：`.batch_id` 与 `.failed`（失败原因清单，CLI 用它判 rc=3）。
    """

    def __init__(self, approved: list[FastlaneRequest], failed: list[str], batch_id: str = ""):
        super().__init__(approved)
        self.approved: list[FastlaneRequest] = list(approved)
        self.failed: list[str] = list(failed)
        self.batch_id: str = batch_id


def plan(
    store: Any,
    round_no: int,
    *,
    summary: str = "",
    package: Any = None,
    files: Optional[list[str]] = None,
    workdir: Optional[str] = None,
) -> FastlaneRequest:
    """**R-33 冻结出口名**：按三判据登记一条请求并落库（`PENDING`；不满足判据 → `ESCALATED`）。

    `files=None` 且拿不到执行器产出 → `no_diff_source` → `ESCALATED`（R-23.2：不猜）。
    自动取 `fl-<round>-<seq>`（可复算，不用 uuid）；`package` 缺省时按 `store.get_package()` 取。
    """
    resolved_package = package
    if resolved_package is None:
        getter = getattr(store, "get_package", None)
        if callable(getter):
            try:
                resolved_package = getter(int(round_no))
            except Exception:  # pragma: no cover - 老库/桩缺实现
                resolved_package = None
    request = make_request(
        int(round_no),
        next_seq(store, int(round_no)),
        summary or _summary_of(resolved_package, int(round_no)),
        package=resolved_package,
        files=files,
        workdir=workdir,
    )
    return save_request(store, request)


def batch_ack(
    store: Any,
    request_ids: list[str],
    by: str = "",
    *,
    batch_id: str = "",
    now: Optional[float] = None,
) -> BatchAckResult:
    """攒批承认（**R-33 冻结签名**）：同批共享一个 `batch_id`；单条失败不拖垮整批。

    返回 `BatchAckResult`（**`list[FastlaneRequest]`**，附带 `.batch_id` / `.failed`）。
    幂等条目（已 approved）也计入成功且保持原批次。
    """
    ids = [str(x).strip() for x in request_ids if str(x).strip()]
    if not ids:
        raise FastlaneError("[fastlane] 批次为空 → 检查 --file 里的 request_id 清单")
    shared = str(batch_id or "").strip() or next_batch_id(store, _round_of(store, ids[0]))
    done: list[FastlaneRequest] = []
    failed: list[str] = []
    for rid in ids:
        try:
            done.append(ack(store, rid, by, batch_id=shared, now=now))
        except FastlaneError as exc:  # 单条失败（状态冲突/查无此单）不影响其余
            failed.append(f"{rid}：{exc}")
    return BatchAckResult(done, failed, batch_id=shared)


def _round_of(store: Any, request_id: str) -> int:
    got = get_request(store, request_id)
    return int(got.round_no) if got is not None else 1


# ---------------------------------------------------------------- CLI


def _default_user() -> str:
    """承认人默认值：`$USER`（Windows 上是 `$USERNAME`），拿不到给 `unknown`。"""
    for key in ("KFPA_USER", "USER", "USERNAME", "LOGNAME"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _store_for(db: str) -> Any:
    """Store 工厂（兼容 P0 的 Store 类）。"""
    try:
        from .store import make_store

        store = make_store(db)
    except (ImportError, AttributeError):  # pragma: no cover - 老环境兜底
        from .store import Store

        store = Store(db)
    init = getattr(store, "init_schema", None)
    if callable(init):
        init()
    return store


def _summary_of(package: Any, round_no: int) -> str:
    if package is None:
        return f"第 {round_no} 轮快车道登记（无任务包，仅登记）"
    feature = str(getattr(package, "feature", "") or "").strip()
    goal = " ".join(str(getattr(package, "goal", "") or "").split())
    head = feature or f"round-{round_no}"
    return (head + "：" + goal[:120]) if goal else head


def _status_row(request: FastlaneRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "round_no": request.round_no,
        "status": request.status.value,
        "summary": request.summary,
        "diff_files": list(request.diff_files),
        "new_interface": bool(request.new_interface),
        "new_data_model": bool(request.new_data_model),
        "touched_modules": list(request.touched_modules),
        "batch_id": request.batch_id,
        "decided_by": request.decided_by,
        "decided_at": request.decided_at,
        "note": request.note,
        "escalations": escalate_reasons(request),
    }


def _cmd_plan(args: Any) -> int:
    store = _store_for(args.db)
    files = None
    if getattr(args, "files", None):
        files = [p.strip() for p in str(args.files).replace("；", ",").split(",") if p.strip()]
    request = plan(
        store,
        int(args.round_no),
        summary=getattr(args, "summary", "") or "",
        files=files,
        workdir=getattr(args, "workdir", None),
    )
    if args.json:
        print(dumps(_status_row(request)))
        return EXIT_OK
    print(f"{request.request_id}｜第 {request.round_no} 轮｜状态 {request.status.value}")
    print(f"  摘要：{request.summary}")
    print(f"  改动文件（R-23 三来源）：{'、'.join(request.diff_files) or '（无来源）'}")
    print(f"  判据：new_interface={request.new_interface} new_data_model={request.new_data_model} "
          f"touched_modules={request.touched_modules or '（无）'}")
    for reason in escalate_reasons(request):
        print(f"  ! 不满足：{reason}")
    if request.status is FastlaneStatus.ESCALATED:
        print("  → 升级回完整设计回合（design.md §5.6）")
    else:
        print(f"  → 待承认：`kfpa fastlane ack {request.request_id} --by <name>`")
    return EXIT_OK


def _cmd_list(args: Any) -> int:
    store = _store_for(args.db)
    rows = [_status_row(r) for r in list_requests(store, getattr(args, "status", None))]
    if args.json:
        print(dumps({"requests": rows, "count": len(rows)}))
        return EXIT_OK
    if not rows:
        print("（无快车道请求）")
        return EXIT_OK
    print(f"{'request_id':<12}{'轮次':<6}{'状态':<11}{'批次':<14}{'承认人':<10}摘要")
    for row in rows:
        print(f"{row['request_id']:<12}{row['round_no']:<6}{row['status']:<11}"
              f"{(row['batch_id'] or '-'):<14}{(row['decided_by'] or '-'):<10}{row['summary'][:40]}")
    return EXIT_OK


def _cmd_ack(args: Any) -> int:
    store = _store_for(args.db)
    try:
        request = ack(store, args.request_id, by=args.by, batch_id=getattr(args, "batch", "") or "")
    except FastlaneConflict as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_MANUAL
    except FastlaneError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if getattr(args, "json", False):
        print(dumps(_status_row(request)))
        return EXIT_OK
    print(f"{request.request_id}：{request.status.value}｜batch={request.batch_id}｜"
          f"by={request.decided_by}｜decided_at={request.decided_at:.3f}")
    return EXIT_OK


def _cmd_batch_ack(args: Any) -> int:
    store = _store_for(args.db)
    path = Path(args.file)
    if not path.is_file():
        print(f"[fastlane] 承认清单不存在：{path} → 用 `--file acks.yaml`（或 JSON）指定批次清单",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[fastlane] 读取承认清单失败：{path}（{exc}）", file=sys.stderr)
        return EXIT_IO
    except UnicodeDecodeError as exc:
        print(f"[fastlane] 承认清单不是 UTF-8 文本：{path}（{exc}）→ {_SHAPE_HINT}",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        ids = load_ack_ids(raw)
    except FastlaneError as exc:  # R-34：坏形状 → rc=2 + 中文（不许静默跳过/补行）
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    try:
        result = batch_ack(store, ids, args.by, batch_id=getattr(args, "batch", "") or "")
    except FastlaneError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    failed = list(result.failed)
    if getattr(args, "json", False):
        print(dumps({
            "batch_id": result.batch_id,
            "approved": [_status_row(r) for r in result],
            "failed": failed,
        }))
    else:
        print(f"批次 {result.batch_id or '-'}：承认 {len(result)} 条，失败 {len(failed)} 条")
        for request in result:
            print(f"  ✓ {request.request_id}（by={request.decided_by}）")
        for item in failed:
            print(f"  ✗ {item}")
    return EXIT_OK if not failed else EXIT_MANUAL


def _cmd_show(args: Any) -> int:
    store = _store_for(args.db)
    request = get_request(store, args.request_id)
    if request is None:
        print(f"[fastlane] 查无此单：{args.request_id} → 先 `kfpa fastlane list`",
              file=sys.stderr)
        return EXIT_USAGE
    package = None
    get_package = getattr(store, "get_package", None)
    if callable(get_package):
        package = get_package(int(request.round_no))
    if args.json:
        row = _status_row(request)
        row["micro_spec"] = micro_spec(request, package)
        print(dumps(row))
        return EXIT_OK
    print(f"{request.request_id}｜第 {request.round_no} 轮｜{request.status.value}")
    print(f"  摘要：{request.summary}")
    print(f"  批次：{request.batch_id or '-'}｜承认人：{request.decided_by or '-'}｜"
          f"承认时间：{request.decided_at or '-'}")
    print(micro_spec(request, package))
    return EXIT_OK


#: R-34：`kfpa fastlane` 组唯一 owner 是本模块（`check` 也由这里注册，内部转 `impact.analyze`）。
_FASTLANE_HELP = "快车道：三判据（`check`）+ 登记与攒批承认落库（D5 §3.2）"


def _subparsers_of(parser: Any) -> Optional[argparse._SubParsersAction]:
    for action in getattr(parser, "_actions", []):
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _fastlane_group(subparsers: Any) -> Any:
    """拿到 `fastlane` 子命令组，三种情形都收敛到同一个组：

    1. **R-34：本模块是唯一 owner** → `subparsers.add_parser("fastlane")` 新建；
    2. 磐石 `cli.py` 的 `_GroupProxy` 把 `add_parser("fastlane")` 并进已存在的组 → 返回的
       parser 可能已经有 `_SubParsersAction`，**直接复用，不再 add_subparsers 第二次**
       （同一个 parser 上建两个子解析器会让后建的那个独吞分派）；
    3. 过渡期 `impact.register()` 仍先注册（P1 语义）→ 从 `_name_parser_map` 取现成组复用。

    任一情形下 `check` 都必须在（正常由本模块注册 `_cmd_check`，见 `register()`）。
    """
    name_map = getattr(subparsers, "_name_parser_map", None)
    existing = name_map.get("fastlane") if isinstance(name_map, dict) else None
    if existing is not None:
        group = _subparsers_of(existing)
        return group if group is not None else existing.add_subparsers(
            dest="fastlane_cmd", required=True
        )
    parser = subparsers.add_parser("fastlane", help=_FASTLANE_HELP)
    group = _subparsers_of(parser)
    if group is not None:  # `_GroupProxy` 并入既有组时，组已经建好
        return group
    return parser.add_subparsers(dest="fastlane_cmd", required=True)


def _cmd_check(args: Any) -> int:
    """`fastlane check` 的等价实现（仅在 impact 尚未注册时兜底）；rc 固定 0（R-24.3）。"""
    from . import impact

    store = _store_for(args.db)
    package = store.get_package(int(args.round_no))
    if package is None:
        print(f"[impact] 第 {args.round_no} 轮还没有任务包：先跑 "
              f"`kfpa round --round {args.round_no}`；本轮不判快车道（不编造结论）")
        return EXIT_OK
    files = resolve_diff_files(int(args.round_no), None, getattr(args, "workdir", None))
    if files is None and getattr(args, "files", None):
        files = [p.strip() for p in str(args.files).replace("；", ",").split(",") if p.strip()]
    report = impact.analyze(package, files if files else None)
    if getattr(args, "json", False):
        print(dumps(report))
        return EXIT_OK
    print(f"轮次 {report.round_no}：{report.summary()}")
    for line in report.reasons:
        print("  " + line)
    if report.fast_lane is False:
        print("  → 升级回完整设计回合（design.md §5.6）")
    return EXIT_OK


def register(subparsers: Any) -> None:  # pragma: no cover - 由 cli.py 挂载
    """注册 `kfpa fastlane check|plan|list|ack|batch-ack|show`（**R-34：本模块唯一 owner**）。"""
    group = _fastlane_group(subparsers)
    names = getattr(group, "_name_parser_map", {})

    if "check" not in names:  # R-34：check 由本模块注册；过渡期 impact 已建组时跳过
        check = group.add_parser("check", help="对某轮任务包做三判据影响分析（R-23.2 / D3-3.6）")
        check.add_argument("--round", dest="round_no", type=int, required=True)
        check.add_argument("--db", default="kfpa.db")
        check.add_argument("--files", default=None, help="逗号分隔的改动文件清单（来源②）")
        check.add_argument("--workdir", default=None, help="本轮工作目录（找执行器产出）")
        check.add_argument("--json", action="store_true")
        check.set_defaults(handler=_cmd_check, func=_cmd_check)

    plan = group.add_parser("plan", help="按三判据登记一条快车道请求（落 fastlane_requests）")
    plan.add_argument("--round", dest="round_no", type=int, required=True)
    plan.add_argument("--db", default="kfpa.db")
    plan.add_argument("--files", default=None, help="改动文件清单（来源②；无执行产物时用）")
    plan.add_argument("--workdir", default=None)
    plan.add_argument("--summary", default="", help="微型规格一句话（缺省取任务包 feature/goal）")
    plan.add_argument("--json", action="store_true")

    listing = group.add_parser("list", help="列快车道请求（可按状态过滤）")
    listing.add_argument("--status", choices=[s.value for s in FastlaneStatus], default=None)
    listing.add_argument("--db", default="kfpa.db")
    listing.add_argument("--json", action="store_true")

    ack_p = group.add_parser("ack", help="承认一条请求（幂等；ESCALATED/REJECTED 不许翻白）")
    ack_p.add_argument("request_id")
    ack_p.add_argument("--by", default=_default_user(), help="承认人（默认 $USER）")
    ack_p.add_argument("--batch", default="", help="批次号（默认自动生成，可复算）")
    ack_p.add_argument("--db", default="kfpa.db")
    ack_p.add_argument("--json", action="store_true")

    batch = group.add_parser("batch-ack", help="攒批承认（同批共享 batch_id）")
    batch.add_argument("--file", required=True, help="承认清单（JSON 数组或逐行 request_id）")
    batch.add_argument("--by", default=_default_user())
    batch.add_argument("--batch", default="")
    batch.add_argument("--db", default="kfpa.db")
    batch.add_argument("--json", action="store_true")

    show = group.add_parser("show", help="看单条请求（含微型规格）")
    show.add_argument("request_id")
    show.add_argument("--db", default="kfpa.db")
    show.add_argument("--json", action="store_true")

    for parser, handler in (
        (plan, _cmd_plan),
        (listing, _cmd_list),
        (ack_p, _cmd_ack),
        (batch, _cmd_batch_ack),
        (show, _cmd_show),
    ):
        parser.set_defaults(handler=handler, func=handler)


__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_MANUAL",
    "EXIT_DEP_MISSING",
    "EXIT_IO",
    "NO_DIFF_SOURCE",
    "MICRO_SPEC_TEMPLATE",
    "FastlaneError",
    "FastlaneConflict",
    "BatchAckResult",
    "judge",
    "escalate_reasons",
    "resolve_diff_files",
    "make_request",
    "plan",
    "micro_spec",
    "save_request",
    "get_request",
    "list_requests",
    "update_request",
    "next_seq",
    "next_batch_id",
    "ack",
    "reject",
    "parse_ack_file",
    "load_ack_ids",
    "batch_ack",
    "register",
]

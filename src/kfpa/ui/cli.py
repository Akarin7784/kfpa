"""質問票台账子命令（``kfpa tickets ...``）。

挂载方式：主入口（磐石 ``src/kfpa/cli.py``）在 ``add_subparsers()`` 之后调用
``register(subparsers)`` 即可。为了让两种分发约定都能跑，每个子命令同时设
``handler`` 与 ``func`` 两个默认值（磐石主入口读 ``args.handler``），处理函数签名
``func(args) -> int``，返回进程退出码。不带子命令调用 ``kfpa tickets`` 时给用法提示并返回 2。

命令（docs/dev/p0-plan.md §3.7）：

- ``kfpa tickets list [--round N] [--impact HIGH] [--status OPEN]``：台账列见 D1-8
- ``kfpa tickets close <ticket_id> --answer "<人工确认>"``：关闭并入库，回显票面与结论
- ``kfpa tickets batch-close --file <answers.yaml|json>``：攒批确认，格式与失败规则见 D1-8

退出码（R-1）：``0`` 成功 / ``2`` 用法与参数错误 / ``4`` 依赖与环境错误 / ``5`` I/O 与数据库错误。

D1-8 + R-8/R-9 落地口径：

- 台账列固定 ``ticket_id | round | impact | status | location | architect_preference | answer``；
- ``--impact`` 按 R-8 **下推 Store**（``list_tickets(round_no, status, impact)``），界面不做本地过滤；
- ``batch-close`` 是**事务**（R-9）：未知票号或某条答案为空 → 整体不写 + 退出码 2；
- 已关闭票重复提交 → 幂等跳过并打印原因（不改库），退出码 0；全部成功 → 0；
- 单张 ``close``：未知票号 / 空结论 → 2；已关闭 → 幂等忽略 + 0。

其它约束：只用标准库；``yaml`` 惰性可选；``kfpa.store`` 惰性导入，未就绪时给中文提示（4）。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..schemas import QATicket, TicketImpact, TicketStatus

__all__ = [
    "register",
    "cmd_list",
    "cmd_close",
    "cmd_batch_close",
    "render_tickets",
    "parse_answers",
    "plan_batch",
    "apply_batch",
    "apply_answers",
    "enum_value",
    "impact_label",
    "status_label",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_DEPS",
    "EXIT_IO",
]

#: 默认库路径（D1-10）
DEFAULT_DB = "kfpa.db"

#: 退出码（D1-5）
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DEPS = 4
EXIT_IO = 5

_IMPACT_LABEL = {"high": "高", "medium": "中", "low": "低"}
_STATUS_LABEL = {"open": "未关闭", "closed": "已关闭", "dropped": "已丢弃"}

#: 台账列固定为 D1-8；值为机读原文（high/open…），中文释义走表尾图例
_COLUMNS: tuple[tuple[str, int], ...] = (
    ("ticket_id", 12),
    ("round", 6),
    ("impact", 8),
    ("status", 8),
    ("location", 28),
    ("architect_preference", 22),
    ("answer", 26),
)

_LEGEND = "影响面：high=高 / medium=中 / low=低；状态：open=未关闭 / closed=已关闭 / dropped=已丢弃"

_JSON_SUFFIXES = {".json"}
_YAML_SUFFIXES = {".yaml", ".yml"}
_TICKET_KEYS = ("ticket_id", "id", "票号")
_ANSWER_KEYS = ("answer", "confirm", "确认")


# ------------------------------------------------------------------ 注册


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    """挂 ``--db``；default=SUPPRESS，避免压掉主入口的全局 ``--db``。"""
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help=f"SQLite 库路径（默认 {DEFAULT_DB}）",
    )


def _defaults(parser: argparse.ArgumentParser, handler) -> None:
    """同时设 handler / func：两种主入口分发约定都能命中。"""
    parser.set_defaults(handler=handler, func=handler)


def register(subparsers) -> None:
    """把 ``tickets`` 子命令组注册到主入口的 subparsers 上。"""
    parser = subparsers.add_parser("tickets", help="質問票台账：列出 / 关闭 / 攒批关闭")
    _add_db_option(parser)  # 让 `kfpa tickets --db x` 走到用法提示，而不是 argparse 报错
    group = parser.add_subparsers(
        dest="tickets_command",
        title="tickets 子命令",
        metavar="<list|close|batch-close>",
    )

    p_list = group.add_parser("list", help="列出質問票（默认全部轮次、全部状态）")
    p_list.add_argument("--round", type=int, default=None, metavar="N", help="只看第 N 轮")
    p_list.add_argument(
        "--impact",
        type=_upper,
        choices=[e.value.upper() for e in TicketImpact],
        default=None,
        metavar="IMPACT",
        help="按影响面过滤：HIGH / MEDIUM / LOW",
    )
    p_list.add_argument(
        "--status",
        type=_upper,
        choices=[e.value.upper() for e in TicketStatus],
        default=None,
        metavar="STATUS",
        help="按状态过滤：OPEN / CLOSED / DROPPED",
    )
    _add_db_option(p_list)
    _defaults(p_list, cmd_list)

    p_close = group.add_parser("close", help="关闭单张票并写入人工确认结论")
    p_close.add_argument("ticket_id", metavar="TICKET_ID", help="票号，如 R1-001")
    p_close.add_argument(
        "--answer",
        required=True,
        metavar="TEXT",
        help="人工确认结论（采用了哪个解释 / 补充约束）",
    )
    _add_db_option(p_close)
    _defaults(p_close, cmd_close)

    p_batch = group.add_parser("batch-close", help="按答案文件攒批关闭（保护确认带宽）")
    p_batch.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help="答案文件：.json 或 .yaml/.yml；D1-8 格式 票号→结论，或 [{ticket_id, answer}, ...]",
    )
    _add_db_option(p_batch)
    _defaults(p_batch, cmd_batch_close)

    _defaults(parser, _cmd_tickets_help)


def _upper(value: str) -> str:
    return (value or "").strip().upper()


def _cmd_tickets_help(_args) -> int:
    print(
        "用法：kfpa tickets <list|close|batch-close> ...（详见 kfpa tickets --help）",
        file=sys.stderr,
    )
    return EXIT_USAGE


# ------------------------------------------------------------------ 取值助手


def enum_value(value: Any) -> str:
    """取枚举/字符串的裸值（Store 返回枚举或字符串都能显示）。"""
    raw = getattr(value, "value", value)
    return str(raw)


def impact_label(value: Any) -> str:
    raw = enum_value(value)
    return _IMPACT_LABEL.get(raw, raw)


def status_label(value: Any) -> str:
    raw = enum_value(value)
    return _STATUS_LABEL.get(raw, raw)


def _db_path(args) -> str:
    return getattr(args, "db", None) or DEFAULT_DB


def _open_store(db_path: str):
    """惰性打开 Store；``kfpa.store`` 未就绪时给中文提示并返回 None。

    优先用 P1 的 ``make_store(dsn)`` 工厂（D3 §3.2，PG/SQLite 双驱动），
    没有该工厂时退回 P0 的 ``Store(db_path)`` —— 磐石重构 store 时不至于让台账断在 import 上。
    """
    try:
        from ..store import Store  # type: ignore[import-not-found]
    except ImportError as exc:
        print(
            f"[kfpa-ui] 缺少 kfpa.store（{exc}）→ 该模块由 p0/core 提供，请先合入或安装后再用 tickets 子命令",
            file=sys.stderr,
        )
        return None
    try:
        from ..store import make_store  # type: ignore[attr-defined]
    except ImportError:
        return Store(db_path)
    return make_store(db_path)


def _io_error(action: str, exc: Exception, db_path: str) -> int:
    """I/O 或数据库错误统一提示（D1-10：模块 + 原因 + 下一步）。"""
    print(
        f"[kfpa-ui] {action}失败：{exc} → 检查 --db 路径与文件权限，必要时先跑 `kfpa init --db {db_path}`",
        file=sys.stderr,
    )
    return EXIT_IO


# ------------------------------------------------------------------ list


def _display_width(text: str) -> int:
    """按东亚字符宽度估算显示宽度（中文/全角算 2）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _clip(text: Any, limit: int) -> str:
    """按显示宽度截断，超长补「…」；换行压成空格。"""
    flat = "" if text is None else str(text).replace("\r", " ").replace("\n", " ")
    if _display_width(flat) <= limit:
        return flat
    out = ""
    for ch in flat:
        if _display_width(out + ch) > limit - 1:
            break
        out += ch
    return out + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_tickets(tickets: Iterable[QATicket]) -> str:
    """按 D1-8 的列渲染台账（中文按双宽对齐）；空列表给中文占位。"""
    rows = sorted(tickets, key=lambda t: (getattr(t, "round_no", 0), str(t.ticket_id)))
    if not rows:
        return "（无匹配質問票）"

    body: list[list[str]] = []
    for t in rows:
        body.append(
            [
                _clip(getattr(t, "ticket_id", ""), _COLUMNS[0][1]),
                _clip(getattr(t, "round_no", ""), _COLUMNS[1][1]),
                _clip(enum_value(getattr(t, "impact", "")), _COLUMNS[2][1]),
                _clip(enum_value(getattr(t, "status", "")), _COLUMNS[3][1]),
                _clip(getattr(t, "location", ""), _COLUMNS[4][1]),
                _clip(getattr(t, "architect_preference", ""), _COLUMNS[5][1]),
                _clip(getattr(t, "answer", None), _COLUMNS[6][1]),
            ]
        )

    widths = [
        max(_display_width(header), *(_display_width(row[i]) for row in body))
        for i, (header, _limit) in enumerate(_COLUMNS)
    ]

    lines = ["  ".join(_pad(h, widths[i]) for i, (h, _l) in enumerate(_COLUMNS)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in body:
        lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())

    open_high = [t for t in rows if _is_open_high(t)]
    lines.append("")
    lines.append(_LEGEND)
    summary = f"共 {len(rows)} 张；未关闭高影响票 {len(open_high)} 张"
    if open_high:
        summary += "（存在时 pipeline 拒绝生成任务包，退出码 3）：" + "、".join(
            str(t.ticket_id) for t in open_high
        )
    lines.append(summary)
    return "\n".join(lines)


def _is_open_high(ticket: Any) -> bool:
    """优先用契约自带的 is_open_high()，没有时按枚举值判断。"""
    checker = getattr(ticket, "is_open_high", None)
    if callable(checker):
        return bool(checker())
    return (
        enum_value(getattr(ticket, "impact", "")) == TicketImpact.HIGH.value
        and enum_value(getattr(ticket, "status", "")) == TicketStatus.OPEN.value
    )


def cmd_list(args) -> int:
    db_path = _db_path(args)
    try:
        store = _open_store(db_path)
    except Exception as exc:  # sqlite3.Error：库/目录不可用
        return _io_error("打开数据库", exc, db_path)
    if store is None:
        return EXIT_DEPS
    try:
        status = TicketStatus(args.status.lower()) if args.status else None
        impact = TicketImpact(args.impact.lower()) if args.impact else None
        # R-8：影响面下推到 Store，界面不做本地过滤
        tickets = list(store.list_tickets(round_no=args.round, status=status, impact=impact))
    except Exception as exc:  # sqlite3.Error 等
        return _io_error("读取台账", exc, db_path)
    print(render_tickets(tickets))
    return EXIT_OK


# ------------------------------------------------------------------ close


def cmd_close(args) -> int:
    ticket_id = str(args.ticket_id or "").strip()
    answer = str(args.answer or "").strip()
    if not ticket_id:
        print('[kfpa-ui] 票号不能为空 → 形如 `kfpa tickets close R1-001 --answer "..."`', file=sys.stderr)
        return EXIT_USAGE
    if not answer:
        print("[kfpa-ui] --answer 不能为空 → 人工确认的结论必须写下来", file=sys.stderr)
        return EXIT_USAGE

    db_path = _db_path(args)
    try:
        store = _open_store(db_path)
    except Exception as exc:
        return _io_error("打开数据库", exc, db_path)
    if store is None:
        return EXIT_DEPS

    try:
        ticket = store.get_ticket(ticket_id)
    except Exception as exc:
        return _io_error("查询票面", exc, db_path)

    if ticket is None:
        print(
            f"[kfpa-ui] 票号 {ticket_id} 不存在 → 先跑 `kfpa tickets list --db {db_path}` 核对票号",
            file=sys.stderr,
        )
        return EXIT_USAGE

    status = enum_value(getattr(ticket, "status", ""))
    if status == TicketStatus.CLOSED.value:
        print(
            f"[kfpa-ui] {ticket_id} 已是 closed（幂等忽略，未改动库）→ 如需改结论请另行处理",
            file=sys.stderr,
        )
        return EXIT_OK
    if status != TicketStatus.OPEN.value:
        print(
            f"[kfpa-ui] {ticket_id} 状态为 {status}，只有 open 的票可确认 → 换一张票",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        closed = store.close_ticket(ticket_id, answer) or store.get_ticket(ticket_id) or ticket
    except KeyError as exc:
        print(f"[kfpa-ui] {ticket_id} 在写入前消失（{exc}）→ 重跑 `kfpa tickets list` 核对", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:
        return _io_error("关闭票", exc, db_path)

    print(
        "已关闭：{tid}（第 {rnd} 轮 · {impact}影响 · {status}）".format(
            tid=getattr(closed, "ticket_id", ticket_id),
            rnd=getattr(closed, "round_no", "?"),
            impact=impact_label(getattr(closed, "impact", "")),
            status=status_label(getattr(closed, "status", "")),
        )
    )
    print(f"  位置：{getattr(closed, 'location', '')}")
    print(f"  A  ：{getattr(closed, 'option_a', '')}")
    print(f"  B  ：{getattr(closed, 'option_b', '')}")
    print(f"  主BOT 倾向：{getattr(closed, 'architect_preference', '')}")
    print(f"  人工确认：{getattr(closed, 'answer', None) or answer}")
    return EXIT_OK


# ------------------------------------------------------------------ batch-close


def parse_answers(payload: str, suffix: str = "") -> tuple[dict[str, str], list[str]]:
    """解析攒批答案，返回 ``(票号→结论, 问题清单)``。

    D1-8 的主形状是映射 ``{"R1-001": "采用 B"}``；为便于手写也接受列表
    ``[{"ticket_id": "R1-001", "answer": "采用 B"}]``，结论值可以是
    ``{"answer": "...", "reason": "..."}`` 这样的对象。后缀未识别时先按 JSON 解析，失败再按 YAML 试一次。
    空结论会原样保留，由 :func:`plan_batch` 记成「拒绝该条」并让退出码为 2。
    """
    problems: list[str] = []
    text = (payload or "").strip()
    if not text:
        return {}, ["答案文件为空"]

    suffix = (suffix or "").lower()
    data, err = _decode_answers(text, suffix)
    if err is not None:
        return {}, [err]
    return _normalize_answers(data, problems), problems


def _decode_answers(payload: str, suffix: str) -> tuple[Any, Optional[str]]:
    if suffix in _YAML_SUFFIXES:
        return _decode_yaml(payload)
    if suffix in _JSON_SUFFIXES:
        return _decode_json(payload)
    data, err = _decode_json(payload)
    if err is None:
        return data, None
    return _decode_yaml(payload)


def _decode_json(payload: str) -> tuple[Any, Optional[str]]:
    try:
        return json.loads(payload), None
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"


def _decode_yaml(payload: str) -> tuple[Any, Optional[str]]:
    try:
        import yaml  # 惰性可选依赖（由 Chief 统一安装 pyyaml）
    except ImportError:
        return None, "未安装 pyyaml → 请改用 .json 答案文件，或请 Chief 安装 pyyaml"
    try:
        return yaml.safe_load(payload), None
    except Exception as exc:  # yaml.YAMLError 及其它解析异常
        return None, f"YAML 解析失败：{exc}"


def _first_value(mapping: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _answer_text(value: Any) -> str:
    if isinstance(value, dict):
        value = _first_value(value, _ANSWER_KEYS)
    return "" if value is None else str(value).strip()


def _normalize_answers(data: Any, problems: list[str]) -> dict[str, str]:
    answers: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            ticket_id = "" if key is None else str(key).strip()
            if not ticket_id:
                problems.append("有一条答案的票号为空，已跳过")
                continue
            # 空结论不在这里丢：交给 plan_batch 记成「拒绝该条」，退出码才可判定
            answers[ticket_id] = _answer_text(value)
    elif isinstance(data, list):
        for index, item in enumerate(data, 1):
            if not isinstance(item, dict):
                problems.append(f"第 {index} 条不是对象，已跳过")
                continue
            ticket_id = _first_value(item, _TICKET_KEYS)
            ticket_id = "" if ticket_id is None else str(ticket_id).strip()
            if not ticket_id:
                problems.append(f"第 {index} 条缺少票号（ticket_id），已跳过")
                continue
            answers[ticket_id] = _answer_text(_first_value(item, _ANSWER_KEYS))
    else:
        problems.append("答案文件顶层必须是对象（票号→结论）或数组")
    return answers


@dataclass
class BatchPlan:
    """攒批预检结果：先全部校验，再决定写不写（D1-8 事务规则）。"""

    to_close: dict[str, str] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)   # 已关闭 → 幂等忽略
    unknown: list[str] = field(default_factory=list)   # 未知票号 → 整体不写
    rejected: list[str] = field(default_factory=list)  # 空结论 / 非 open 状态 → 拒绝该条

    @property
    def writable(self) -> bool:
        return not self.unknown


def plan_batch(store, answers: dict[str, str]) -> BatchPlan:
    """逐条预检，**不写库**；``unknown`` / ``rejected`` 都是输入错误，调用方整体不写。"""
    plan = BatchPlan()
    for ticket_id, answer in answers.items():
        text = str(answer or "").strip()
        if not text:
            plan.rejected.append(f"{ticket_id}：结论为空")
            continue
        ticket = store.get_ticket(ticket_id)
        if ticket is None:
            plan.unknown.append(ticket_id)
            continue
        status = enum_value(getattr(ticket, "status", ""))
        if status == TicketStatus.OPEN.value:
            plan.to_close[ticket_id] = text
        elif status == TicketStatus.CLOSED.value:
            plan.ignored.append(ticket_id)
        else:
            plan.rejected.append(f"{ticket_id}：状态 {status}，不可确认")
    return plan


def apply_batch(store, plan: BatchPlan) -> list[str]:
    """只写 ``plan.to_close``（预检通过的部分），返回已关闭票号。"""
    closed: list[str] = []
    for ticket_id, answer in plan.to_close.items():
        store.close_ticket(ticket_id, answer)
        closed.append(ticket_id)
    return closed


def apply_answers(store, answers: dict[str, str]) -> tuple[list[str], list[str]]:
    """页面用的一次性封装：未知票号或空结论则整体不写（R-9）；返回 ``(已关闭, 提示)``。"""
    plan = plan_batch(store, answers)
    if plan.unknown or plan.rejected:
        notes = [f"{t}：未知票号" for t in plan.unknown]
        notes += [f"拒绝：{r}" for r in plan.rejected]
        return [], [n + "，按 R-9 本次不写任何票" for n in notes]
    closed = apply_batch(store, plan)
    return closed, [f"跳过 {t}：已关闭（幂等忽略）" for t in plan.ignored]


def cmd_batch_close(args) -> int:
    path = Path(args.file)
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        print(
            f"[kfpa-ui] 答案文件不存在：{path}（{exc}）→ 按 D1-8 格式准备文件后重跑",
            file=sys.stderr,
        )
        return EXIT_DEPS
    except OSError as exc:
        print(f"[kfpa-ui] 读不到答案文件 {path}：{exc} → 检查路径与 UTF-8 编码", file=sys.stderr)
        return EXIT_IO

    answers, problems = parse_answers(payload, path.suffix)
    for problem in problems:
        print(f"[提示] {problem}", file=sys.stderr)
    if not answers:
        print(
            "[kfpa-ui] 答案文件里没有可用的「票号→结论」→ 按 D1-8 格式填写后重跑",
            file=sys.stderr,
        )
        return EXIT_USAGE

    db_path = _db_path(args)
    try:
        store = _open_store(db_path)
    except Exception as exc:
        return _io_error("打开数据库", exc, db_path)
    if store is None:
        return EXIT_DEPS

    try:
        plan = plan_batch(store, answers)
    except Exception as exc:
        return _io_error("预检攒批答案", exc, db_path)

    # R-9：未知票号 / 空结论等输入错误 → 整体不写（事务）
    if plan.unknown or plan.rejected:
        if plan.unknown:
            print(
                "[kfpa-ui] 未知票号："
                + "、".join(plan.unknown)
                + f" → 先跑 `kfpa tickets list --db {db_path}` 核对票号",
                file=sys.stderr,
            )
        for reason in plan.rejected:
            print(f"[拒绝] {reason}", file=sys.stderr)
        print("[kfpa-ui] 按 R-9 本次整体不写（事务）", file=sys.stderr)
        return EXIT_USAGE

    try:
        closed = apply_batch(store, plan)
    except Exception as exc:
        return _io_error("写入攒批结论", exc, db_path)

    for ticket_id in closed:
        print(f"已关闭 {ticket_id} → {plan.to_close[ticket_id]}")
    for ticket_id in plan.ignored:
        print(f"跳过 {ticket_id}：已关闭（幂等忽略，未改动库）")
    print(f"攒批完成：关闭 {len(closed)} 张，跳过 {len(plan.ignored)} 张。")
    return EXIT_OK

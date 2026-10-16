"""質問票台账子命令（``kfpa tickets ...``）。

主入口（磐石 ``src/kfpa/cli.py``）只要在 ``add_subparsers()`` 之后调用本模块的
``register(subparsers)`` 即可；各子命令用 ``set_defaults(func=...)`` 挂处理函数，
约定签名为 ``func(args) -> int``（返回进程退出码）。若不带子命令调用
``kfpa tickets``，本模块给用法提示并返回 2。

命令（docs/dev/p0-plan.md §3.7）：

- ``kfpa tickets list [--round N] [--impact HIGH] [--status OPEN]``
- ``kfpa tickets close <ticket_id> --answer "<人工确认>"``
- ``kfpa tickets batch-close --file <answers.yaml|json>``

实现约束：

- 只用标准库；``yaml`` 是惰性可选依赖（缺包时给中文提示并建议改用 JSON）；
- ``kfpa.store`` 惰性导入：``store.py`` 由 p0/core（磐石）提供，尚未合入时
  必须给中文提示，而不是 ImportError 崩溃；
- ``--db`` 的 default 用 ``argparse.SUPPRESS``：主入口若已定义全局 ``--db``，
  子命令不写它时不覆盖，写它时（如 ``tickets list --db t.db``）以子命令为准；
- 影响面过滤在本地做：冻结接口 ``Store.list_tickets(round_no, status)`` 没有
  ``impact`` 参数（``schemas`` 与 §3.2 只读），界面不擅自扩契约。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
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
    "apply_answers",
    "impact_label",
    "status_label",
]

#: 默认库路径，与主入口 §3.8 一致
DEFAULT_DB = "kfpa.db"

_IMPACT_LABEL = {"high": "高", "medium": "中", "low": "低"}
_STATUS_LABEL = {"open": "未关闭", "closed": "已关闭", "dropped": "已丢弃"}

#: list 表格的列（表头, 最大显示宽度）：票号/轮次/位置/影响面/状态/主BOT倾向
_COLUMNS: tuple[tuple[str, int], ...] = (
    ("票号", 18),
    ("轮次", 6),
    ("位置", 30),
    ("影响面", 8),
    ("状态", 8),
    ("倾向", 26),
)

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


def register(subparsers) -> None:
    """把 ``tickets`` 子命令组注册到主入口的 subparsers 上。"""
    parser = subparsers.add_parser("tickets", help="質問票台账：列出 / 关闭 / 攒批关闭")
    _add_db_option(parser)  # 让 `kfpa tickets --db x` 也能走到用法提示，而不是 argparse 报错
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
    p_list.set_defaults(func=cmd_list)

    p_close = group.add_parser("close", help="关闭单张票并写入人工确认结论")
    p_close.add_argument("ticket_id", metavar="TICKET_ID", help="票号，如 T-001")
    p_close.add_argument(
        "--answer",
        required=True,
        metavar="TEXT",
        help="人工确认结论（采用了哪个解释 / 补充约束）",
    )
    _add_db_option(p_close)
    p_close.set_defaults(func=cmd_close)

    p_batch = group.add_parser("batch-close", help="按答案文件攒批关闭（保护确认带宽）")
    p_batch.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help="答案文件：.json 或 .yaml/.yml；形如 {票号: 结论} 或 [{ticket_id, answer}, ...]",
    )
    _add_db_option(p_batch)
    p_batch.set_defaults(func=cmd_batch_close)

    # 不带子命令时（kfpa tickets）给用法提示，而不是 KeyError/AttributeError
    parser.set_defaults(func=_cmd_tickets_help)


def _upper(value: str) -> str:
    return (value or "").strip().upper()


def _cmd_tickets_help(_args) -> int:
    print(
        "用法：kfpa tickets <list|close|batch-close> ...（详见 kfpa tickets --help）",
        file=sys.stderr,
    )
    return 2


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
    """惰性打开 Store；``kfpa.store`` 未就绪时给中文提示并返回 None。"""
    try:
        from ..store import Store  # type: ignore[import-not-found]
    except ImportError as exc:
        print(
            f"[kfpa-ui] 无法加载 kfpa.store（{exc}）：该模块由 p0/core（磐石）提供，"
            "请先合入后再使用 tickets 子命令。",
            file=sys.stderr,
        )
        return None
    return Store(db_path)


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
    """把票渲染成等宽表格（中文按双宽对齐）；空列表给中文占位。"""
    rows = sorted(tickets, key=lambda t: (getattr(t, "round_no", 0), str(t.ticket_id)))
    if not rows:
        return "（无匹配質問票）"

    body: list[list[str]] = []
    for t in rows:
        body.append(
            [
                _clip(getattr(t, "ticket_id", ""), _COLUMNS[0][1]),
                _clip(getattr(t, "round_no", ""), _COLUMNS[1][1]),
                _clip(getattr(t, "location", ""), _COLUMNS[2][1]),
                _clip(impact_label(getattr(t, "impact", "")), _COLUMNS[3][1]),
                _clip(status_label(getattr(t, "status", "")), _COLUMNS[4][1]),
                _clip(getattr(t, "architect_preference", ""), _COLUMNS[5][1]),
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
    summary = f"共 {len(rows)} 张；未关闭高影响票 {len(open_high)} 张"
    if open_high:
        summary += "（存在时 pipeline 拒绝生成任务包）：" + "、".join(
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
    store = _open_store(_db_path(args))
    if store is None:
        return 2
    status = TicketStatus(args.status.lower()) if args.status else None
    tickets = list(store.list_tickets(round_no=args.round, status=status))
    if args.impact:
        want = args.impact.lower()
        tickets = [t for t in tickets if enum_value(getattr(t, "impact", "")) == want]
    print(render_tickets(tickets))
    return 0


# ------------------------------------------------------------------ close


def cmd_close(args) -> int:
    ticket_id = str(args.ticket_id or "").strip()
    answer = str(args.answer or "").strip()
    if not ticket_id:
        print("[kfpa-ui] 票号不能为空。", file=sys.stderr)
        return 2
    if not answer:
        print("[kfpa-ui] --answer 不能为空：人工确认的结论必须写下来。", file=sys.stderr)
        return 2

    store = _open_store(_db_path(args))
    if store is None:
        return 2

    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        print(f"[kfpa-ui] 库里没有票号 {ticket_id}（可先跑 kfpa tickets list）。", file=sys.stderr)
        return 1
    if enum_value(getattr(ticket, "status", "")) != TicketStatus.OPEN.value:
        print(
            f"[kfpa-ui] {ticket_id} 当前状态「{status_label(ticket.status)}」，"
            "只有未关闭（OPEN）的票才能确认。",
            file=sys.stderr,
        )
        return 1

    closed = store.close_ticket(ticket_id, answer) or store.get_ticket(ticket_id) or ticket
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
    return 0


# ------------------------------------------------------------------ batch-close


def parse_answers(payload: str, suffix: str = "") -> tuple[dict[str, str], list[str]]:
    """解析攒批答案，返回 ``(票号→结论, 问题清单)``。

    支持两种形状：

    - 映射：``{"T-001": "采用 A"}``
    - 列表：``[{"ticket_id": "T-001", "answer": "采用 A"}]``

    结论值也可以是 ``{"answer": "...", "reason": "..."}`` 这样的对象。
    后缀未识别时先按 JSON 解析，失败再按 YAML 试一次。
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
        return None, "未安装 pyyaml，无法解析 YAML；请改用 .json 答案文件（或请 Chief 安装 pyyaml）"
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
            answer = _answer_text(value)
            if not ticket_id:
                problems.append("有一条答案的票号为空，已跳过")
                continue
            if not answer:
                problems.append(f"{ticket_id}：结论为空，已跳过")
                continue
            answers[ticket_id] = answer
    elif isinstance(data, list):
        for index, item in enumerate(data, 1):
            if not isinstance(item, dict):
                problems.append(f"第 {index} 条不是对象，已跳过")
                continue
            ticket_id = _first_value(item, _TICKET_KEYS)
            ticket_id = "" if ticket_id is None else str(ticket_id).strip()
            answer = _answer_text(_first_value(item, _ANSWER_KEYS))
            if not ticket_id:
                problems.append(f"第 {index} 条缺少票号（ticket_id），已跳过")
                continue
            if not answer:
                problems.append(f"{ticket_id}：结论为空，已跳过")
                continue
            answers[ticket_id] = answer
    else:
        problems.append("答案文件顶层必须是对象（票号→结论）或数组")
    return answers


def apply_answers(store, answers: dict[str, str]) -> tuple[list[str], list[str]]:
    """逐票关闭，返回 ``(已关闭票号, 失败说明)``；不抛异常，逐条给出原因。"""
    closed: list[str] = []
    failures: list[str] = []
    for ticket_id, answer in answers.items():
        ticket = store.get_ticket(ticket_id)
        if ticket is None:
            failures.append(f"{ticket_id}：库中不存在该票号，已跳过")
            continue
        if enum_value(getattr(ticket, "status", "")) != TicketStatus.OPEN.value:
            failures.append(
                f"{ticket_id}：当前状态「{status_label(ticket.status)}」，只有未关闭的票可确认，已跳过"
            )
            continue
        store.close_ticket(ticket_id, answer)
        closed.append(ticket_id)
    return closed, failures


def cmd_batch_close(args) -> int:
    path = Path(args.file)
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[kfpa-ui] 读不到答案文件 {path}：{exc}", file=sys.stderr)
        return 2

    answers, problems = parse_answers(payload, path.suffix)
    for problem in problems:
        print(f"[提示] {problem}", file=sys.stderr)
    if not answers:
        print("[kfpa-ui] 答案文件里没有可用的「票号→结论」，未做任何关闭。", file=sys.stderr)
        return 2

    store = _open_store(_db_path(args))
    if store is None:
        return 2

    closed, failures = apply_answers(store, answers)
    for ticket_id in closed:
        print(f"已关闭 {ticket_id} → {answers[ticket_id]}")
    for failure in failures:
        print(f"[跳过] {failure}", file=sys.stderr)
    print(f"攒批完成：关闭 {len(closed)} 张，跳过 {len(failures)} 张。")
    return 0 if not failures else 1

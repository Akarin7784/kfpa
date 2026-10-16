"""KFPA 質問票台账页面（P1：真跑 + headless 冒烟 + 快车道承认页）。

用法::

    # 真跑（浏览器）
    streamlit run src/kfpa/ui/streamlit_app.py -- --db kfpa.db

    # headless（A17 验收路径：不进浏览器，返回页面数据）
    $env:PYTHONPATH="src"
    python -c "from kfpa.ui.streamlit_app import main; raise SystemExit(main(['--headless','--db','kfpa.db','--json']))"

约束（docs/dev/p0-plan.md §3.7 / docs/dev/p1-plan.md §3.7、A17）：

- ``streamlit`` 惰性导入：缺包 → 中文提示 + rc=4（D1-5），不抛 ImportError；
- 台账列与攒批事务规则**不变**（D1-8 / R-9），攒批复用 ``kfpa.ui.cli`` 的解析与落库；
- 快车道承认页在 ``kfpa/ui/fastlane.py``（D3 §2/§3.6/§3.7），判定走 ``kfpa.impact.analyze``；
- 数据层（``load_ticket_rows`` / ``fastlane.load_reports``）不 import streamlit，可直接 headless 断言；
- 页面数据落在 ``st.session_state["kfpa_rows"]``，``render_headless()`` 用它作为返回的表格数据。

退出码：``0`` 成功 / ``2`` 用法 / ``4`` 依赖缺失（streamlit 等）/ ``5`` I/O 或库错误。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB = "kfpa.db"

#: 退出码（D1-5 / R-1）
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DEPS = 4
EXIT_IO = 5

#: 命令行不给 --db 时的兜底环境变量（headless 渲染用）
_ENV_DB = "KFPA_UI_DB"
#: 快车道 diff 文件清单（JSON：{"1": ["src/kfpa/gates.py"]}）与承认留痕文件
_ENV_DIFF_FILES = "KFPA_UI_DIFF_FILES"
_ENV_ACK_FILE = "KFPA_UI_ACK_FILE"
#: render_headless() 运行 AppTest 时的标记：让 __main__ 不 raise SystemExit（见该函数）
_ENV_APPTEST = "KFPA_UI_APPTEST"

_APP_PATH = Path(__file__).resolve()
_PAGE_TITLE = "KFPA 質問票台账"

_STREAMLIT_HINT = (
    "[kfpa-ui] 未安装 streamlit（D1-5 依赖缺失）→ 台账页面与 headless 渲染都不可用。\n"
    "[kfpa-ui] 由 Chief 统一安装：pip install streamlit；"
    "或先用命令行台账 `kfpa tickets list|close|batch-close`。"
)


class UIDependencyError(RuntimeError):
    """依赖缺失（对应 rc=4）。"""


class UIUsageError(RuntimeError):
    """用法/参数错误（对应 rc=2）。"""


class UIOperationError(RuntimeError):
    """I/O 或数据库错误（对应 rc=5）。"""


# ------------------------------------------------------------------ 基础


def _ensure_src_on_path() -> None:
    """``streamlit run`` 直接把本文件当脚本执行，先把 src 挂上 sys.path。"""
    try:
        import kfpa  # noqa: F401
    except ImportError:
        src = Path(__file__).resolve().parents[2]
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))


def _env_db() -> str:
    return os.environ.get(_ENV_DB) or DEFAULT_DB


def _db_from_argv(argv: Optional[list[str]] = None) -> str:
    """从 ``-- --db PATH`` 取库路径；忽略 streamlit 自己的参数。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", default=None)
    known, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return known.db or _env_db()


def _open_store(db_path: str):
    """惰性打开 Store（磐石）；缺模块 → UIDependencyError，库错 → UIOperationError。

    优先用 P1 的 ``make_store(dsn)`` 工厂（D3 §3.2，PG/SQLite 双驱动），
    没有该工厂时退回 P0 的 ``Store(db_path)``。
    """
    try:
        from kfpa.store import Store  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UIDependencyError(
            f"[kfpa-ui] 缺少 kfpa.store（{exc}）→ 该模块归磐石，合入后再用本页面。"
        ) from exc
    try:
        from kfpa.store import make_store  # type: ignore[attr-defined]
    except ImportError:
        make_store = None  # type: ignore[assignment]
    try:
        return make_store(db_path) if make_store is not None else Store(db_path)
    except Exception as exc:
        raise UIOperationError(
            f"[kfpa-ui] 打开数据库失败：{exc} → 检查 --db {db_path}，必要时先跑 `kfpa init --db {db_path}`"
        ) from exc


# ------------------------------------------------------------------ 数据层（无 streamlit 依赖）


def _list_tickets(store, round_no: Optional[int] = None, status=None, impact=None) -> list[Any]:
    try:
        # R-8：影响面下推到 Store，与 CLI 同一口径
        return list(store.list_tickets(round_no=round_no, status=status, impact=impact))
    except Exception as exc:
        raise UIOperationError(f"[kfpa-ui] 读取台账失败：{exc} → 检查 --db 路径与库表") from exc


def _ticket_rows(raw: list[Any]) -> list[dict[str, Any]]:
    from kfpa.ui.cli import enum_value, impact_label, status_label

    return [
        {
            "票号": getattr(t, "ticket_id", ""),
            "轮次": getattr(t, "round_no", ""),
            "位置": getattr(t, "location", ""),
            "影响面": impact_label(getattr(t, "impact", "")),
            "状态": status_label(getattr(t, "status", "")),
            "主BOT 倾向": getattr(t, "architect_preference", ""),
            "人工确认": getattr(t, "answer", "") or "",
            "impact_raw": enum_value(getattr(t, "impact", "")),
            "status_raw": enum_value(getattr(t, "status", "")),
        }
        for t in raw
    ]


def load_ticket_rows(
    db_path: str = DEFAULT_DB,
    *,
    round_no: Optional[int] = None,
    status=None,
    impact=None,
    store=None,
) -> list[dict[str, Any]]:
    """台账表格数据（A17 的「返回表格数据」就是它）。"""
    return _ticket_rows(_list_tickets(store or _open_store(db_path), round_no, status, impact))


def diff_files_from_env() -> dict[int, list[str]]:
    from kfpa.ui.fastlane import parse_diff_files

    path = os.environ.get(_ENV_DIFF_FILES)
    if not path:
        return {}
    try:
        return parse_diff_files(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}


def collect_page_data(
    db_path: str = DEFAULT_DB,
    *,
    store=None,
    include_fastlane: bool = True,
    diff_files: Optional[dict[int, list[str]]] = None,
) -> dict[str, Any]:
    """页面/headless 共用的数据组装：台账行 + 快车道行 + 提示。"""
    store = store or _open_store(db_path)
    raw = _list_tickets(store)
    rows = _ticket_rows(raw)
    hints: list[str] = []
    fastlane_rows: list[dict[str, Any]] = []
    if include_fastlane:
        try:
            from kfpa.ui import fastlane

            fastlane_rows, hint = fastlane.load_reports(
                store, diff_files_by_round=diff_files if diff_files is not None else diff_files_from_env()
            )
            if hint:
                hints.append(hint)
        except Exception as exc:  # 快车道页坏掉不拖垮台账
            hints.append(f"[kfpa-ui] 快车道页加载失败：{exc} → 先看 kfpa tickets list 是否正常")
    return {
        "db": str(db_path),
        "tickets": rows,
        "fastlane": fastlane_rows,
        "hints": hints,
        "open_high": sum(1 for r in rows if r["impact_raw"] == "high" and r["status_raw"] == "open"),
    }


# ------------------------------------------------------------------ 页面渲染


def page(
    st,
    db_path: Optional[str] = None,
    *,
    include_fastlane: bool = True,
    ack_file: Optional[str] = None,
) -> dict[str, Any]:
    """渲染整页；返回页面数据（同时写入 ``st.session_state["kfpa_rows"]``）。"""
    from kfpa.ui.cli import (
        TicketImpact,
        TicketStatus,
        apply_answers,
        enum_value,
        impact_label,
        parse_answers,
        status_label,
    )

    db = db_path or _env_db()
    st.set_page_config(page_title=_PAGE_TITLE, layout="wide")
    st.title(_PAGE_TITLE)
    st.caption(
        "P1：質問票台账 + 快车道承认。台账列与攒批事务同 D1-8 / R-9；"
        "未关闭高影响票会挡住任务包生成（退出码 3）。"
    )

    store = _open_store(db)
    raw = _list_tickets(store)
    data = collect_page_data(db, store=store, include_fastlane=include_fastlane)
    st.session_state["kfpa_rows"] = data

    for hint in data["hints"]:
        st.info(hint)

    with st.sidebar:
        st.header("筛选")
        rounds = sorted({getattr(t, "round_no", 0) for t in raw})
        picked_round = st.selectbox("轮次", ["全部", *[str(r) for r in rounds]])
        impact_options = [e.value for e in TicketImpact]
        picked_impact = st.multiselect("影响面", impact_options, default=impact_options)
        status_options = [e.value for e in TicketStatus]
        picked_status = st.multiselect("状态", status_options, default=status_options)
        st.caption(f"库路径：{db}")

    def _keep(ticket: Any) -> bool:
        if picked_round != "全部" and str(getattr(ticket, "round_no", "")) != picked_round:
            return False
        if enum_value(getattr(ticket, "impact", "")) not in picked_impact:
            return False
        return enum_value(getattr(ticket, "status", "")) in picked_status

    shown = [t for t in raw if _keep(t)]
    open_high = [
        t
        for t in raw
        if enum_value(getattr(t, "impact", "")) == "high"
        and enum_value(getattr(t, "status", "")) == "open"
    ]

    left, right = st.columns([3, 1])
    left.metric("票数（筛选后 / 全部）", f"{len(shown)} / {len(raw)}")
    right.metric("未关闭高影响票", len(open_high))

    st.dataframe(_ticket_rows(shown), width="stretch")

    st.divider()

    st.subheader("单张确认")
    open_tickets = [t for t in raw if enum_value(getattr(t, "status", "")) == "open"]
    if not open_tickets:
        st.info("没有未关闭的票。")
    else:
        ids = [str(t.ticket_id) for t in open_tickets]
        picked = st.selectbox("票号", ids)
        chosen = next(t for t in open_tickets if str(t.ticket_id) == picked)
        st.markdown(
            f"- 位置：`{chosen.location}`\n"
            f"- A：{chosen.option_a}\n"
            f"- B：{chosen.option_b}\n"
            f"- 主BOT 倾向：{chosen.architect_preference}"
        )
        answer = st.text_area("人工确认结论", key=f"answer-{picked}")
        if st.button("关闭该票", type="primary"):
            if not answer.strip():
                st.warning("结论不能为空。")
            else:
                store.close_ticket(picked, answer.strip())
                st.success(f"已关闭 {picked}")
                st.rerun()

    st.subheader("攒批确认（答案文件）")
    uploaded = st.file_uploader("answers.yaml / answers.json", type=["yaml", "yml", "json"])
    if uploaded is not None:
        payload = uploaded.getvalue().decode("utf-8")
        answers, problems = parse_answers(payload, Path(uploaded.name).suffix)
        for problem in problems:
            st.warning(problem)
        if answers:
            st.dataframe(
                [{"票号": tid, "结论": ans} for tid, ans in answers.items()],
                width="stretch",
            )
            if st.button(f"批量关闭 {len(answers)} 张"):
                closed, failures = apply_answers(store, answers)
                st.success(f"已关闭 {len(closed)} 张：" + "、".join(closed))
                for failure in failures:
                    st.warning(failure)
                st.rerun()

    if include_fastlane:
        from kfpa.ui import fastlane

        ack_path = ack_file or os.environ.get(_ENV_ACK_FILE)

        def _ack(rounds: list[int], note: str) -> dict[str, Any]:
            return fastlane.acknowledge(
                data["fastlane"], rounds, actor="streamlit", note=note, ack_file=ack_path
            )

        fastlane.render(st, data["fastlane"], ack=_ack)

    st.caption(
        "台账列 / 攒批事务：D1-8 · R-9；快车道判据：D3 §3.6；"
        f"契约 {_contract_version()}。"
    )
    return data


def _contract_version() -> str:
    try:
        from kfpa.schemas import CONTRACT_VERSION

        return str(CONTRACT_VERSION)
    except Exception:
        return "unknown"


# ------------------------------------------------------------------ headless


def render_headless(
    db_path: str = DEFAULT_DB,
    *,
    include_fastlane: bool = True,
    timeout: int = 60,
) -> dict[str, Any]:
    """用 ``streamlit.testing.v1.AppTest`` 真渲染一次（不进浏览器），返回页面数据。"""
    _ensure_src_on_path()
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError as exc:
        raise UIDependencyError(_STREAMLIT_HINT) from exc

    os.environ[_ENV_DB] = str(db_path or DEFAULT_DB)
    if not include_fastlane:
        os.environ["KFPA_UI_NO_FASTLANE"] = "1"
    else:
        os.environ.pop("KFPA_UI_NO_FASTLANE", None)

    # AppTest 里脚本以 __main__ 执行；末尾 raise SystemExit 会让 AppTest 的
    # 「脚本无错」内部键拿不到（KeyError），用这个标记让 __main__ 不抛
    previous = os.environ.get(_ENV_APPTEST)
    os.environ[_ENV_APPTEST] = "1"
    try:
        app = AppTest.from_file(str(_APP_PATH), default_timeout=timeout).run()
    finally:
        if previous is None:
            os.environ.pop(_ENV_APPTEST, None)
        else:
            os.environ[_ENV_APPTEST] = previous

    if app.exception:
        messages = []
        for item in app.exception:
            value = getattr(item, "value", item)
            messages.append(f"{type(value).__name__}: {value}")
        raise UIOperationError("页面渲染异常：" + "；".join(messages))

    data = app.session_state.get("kfpa_rows")
    if not data:
        raise UIOperationError("[kfpa-ui] 页面没有产出 kfpa_rows（检查 page() 是否被调用）")
    return dict(data)


# ------------------------------------------------------------------ 入口


def _parse(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kfpa-ui-streamlit",
        description="KFPA 台账页面（真跑 / headless 渲染）",
    )
    parser.add_argument("--db", default=None, help=f"SQLite 库路径（默认 {DEFAULT_DB}，环境变量 {_ENV_DB}）")
    parser.add_argument("--headless", action="store_true", help="不进浏览器，用 AppTest 渲染一次（A17）")
    parser.add_argument("--json", action="store_true", help="headless 时输出 schemas.dumps 单行 JSON")
    parser.add_argument("--no-fastlane", action="store_true", help="不渲染快车道承认页")
    # streamlit run 会在 sys.argv 里塞脚本路径与自己的参数，未知参数一律忽略
    args, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return args


def main(argv: Optional[list[str]] = None) -> int:
    _ensure_src_on_path()
    args = _parse(argv)
    db_path = args.db or _env_db()
    include_fastlane = not args.no_fastlane and not os.environ.get("KFPA_UI_NO_FASTLANE")

    if args.headless:
        try:
            data = render_headless(db_path, include_fastlane=include_fastlane)
        except UIDependencyError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_DEPS
        except UIOperationError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_IO
        if args.json:
            from kfpa.schemas import dumps

            print(dumps(data))
        else:
            print(
                f"[kfpa-ui] headless 渲染 OK：台账 {len(data.get('tickets', []))} 行，"
                f"快车道 {len(data.get('fastlane', []))} 行，库 {data.get('db')}"
            )
        return EXIT_OK

    try:
        import streamlit as st
    except ImportError:
        print(_STREAMLIT_HINT, file=sys.stderr)
        return EXIT_DEPS

    try:
        page(st, db_path, include_fastlane=include_fastlane)
    except UIDependencyError as exc:
        st.error(str(exc))
        return EXIT_DEPS
    except UIOperationError as exc:
        st.error(str(exc))
        return EXIT_IO
    return EXIT_OK


if __name__ == "__main__":
    _rc = main()
    if not os.environ.get(_ENV_APPTEST):  # AppTest 下不 raise，否则它取不到「脚本无错」键
        raise SystemExit(_rc)

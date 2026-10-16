"""質問票台账页面（可选，**不列入 P0 验收**）。

用法::

    streamlit run src/kfpa/ui/streamlit_app.py -- --db kfpa.db

约束（docs/dev/p0-plan.md §0/§3.7）：

- ``streamlit`` 惰性导入：缺包时给中文提示并返回非零，不抛 ImportError；
- 页面只读 ``kfpa.store.Store``（磐石）并调用其关闭接口，不自己写 SQL；
- 攒批关闭复用 ``kfpa.ui.cli`` 的解析/落库逻辑，保证 CLI 与页面同一套语义；
- 验收仍走 ``kfpa tickets list/close/batch-close``（A7），本页面不参与断言。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_STREAMLIT_HINT = (
    "[kfpa-ui] 未安装 streamlit，質問票台账页面不可用（P0 不列入验收）。\n"
    "[kfpa-ui] 由 Chief 统一安装：pip install streamlit；"
    "验收请走 `kfpa tickets list|close|batch-close`。"
)

_PAGE_TITLE = "KFPA 質問票台账"


def _ensure_src_on_path() -> None:
    """``streamlit run`` 直接把本文件当脚本执行，先把 src 挂上 sys.path。"""
    try:
        import kfpa  # noqa: F401
    except ImportError:
        src = Path(__file__).resolve().parents[2]
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))


def _db_from_argv(argv: list[str] | None = None) -> str:
    """从 ``-- --db PATH`` 取库路径；忽略 streamlit 自己的参数。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", default="kfpa.db")
    known, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return known.db


def _load_deps() -> tuple[Any, Any, Any, Any, Any, Any] | None:
    """惰性加载 streamlit 与项目模块；缺包时返回 None（已打印中文提示）。"""
    try:
        import streamlit as st
    except ImportError:
        print(_STREAMLIT_HINT, file=sys.stderr)
        return None

    try:
        from kfpa.store import Store
    except ImportError as exc:
        st.error(f"无法加载 kfpa.store（{exc}）：该模块由 p0/core（磐石）提供，请先合入。")
        return None

    try:
        from kfpa.schemas import TicketImpact, TicketStatus
        from kfpa.ui.cli import apply_answers, impact_label, parse_answers, status_label
    except ImportError as exc:
        st.error(f"无法加载 kfpa 基础模块：{exc}")
        return None

    return st, Store, TicketImpact, TicketStatus, parse_answers, apply_answers


def _rows(tickets: list[Any], impact_label, status_label) -> list[dict[str, Any]]:
    return [
        {
            "票号": getattr(t, "ticket_id", ""),
            "轮次": getattr(t, "round_no", ""),
            "位置": getattr(t, "location", ""),
            "影响面": impact_label(getattr(t, "impact", "")),
            "状态": status_label(getattr(t, "status", "")),
            "主BOT 倾向": getattr(t, "architect_preference", ""),
            "人工确认": getattr(t, "answer", "") or "",
        }
        for t in tickets
    ]


def _render(st, Store, TicketImpact, TicketStatus, parse_answers, apply_answers) -> int:
    st.set_page_config(page_title=_PAGE_TITLE, layout="wide")
    st.title(_PAGE_TITLE)
    st.caption(
        "设计 → 質問票 → 人工确认；未关闭的高影响票会挡住任务包生成"
        "（docs/dev/p0-plan.md §3.4 硬规则）。"
    )

    db_path = _db_from_argv()
    store = Store(db_path)
    from kfpa.ui.cli import enum_value, impact_label, status_label  # 展示与过滤共用

    tickets = list(store.list_tickets())

    with st.sidebar:
        st.header("筛选")
        rounds = sorted({getattr(t, "round_no", 0) for t in tickets})
        picked_round = st.selectbox("轮次", ["全部", *[str(r) for r in rounds]])
        impact_options = [e.value for e in TicketImpact]
        picked_impact = st.multiselect("影响面", impact_options, default=impact_options)
        status_options = [e.value for e in TicketStatus]
        picked_status = st.multiselect("状态", status_options, default=status_options)
        st.caption(f"库路径：{db_path}")

    def _keep(t: Any) -> bool:
        if picked_round != "全部" and str(getattr(t, "round_no", "")) != picked_round:
            return False
        if enum_value(getattr(t, "impact", "")) not in picked_impact:
            return False
        return enum_value(getattr(t, "status", "")) in picked_status

    shown = [t for t in tickets if _keep(t)]
    open_high = [
        t
        for t in tickets
        if enum_value(getattr(t, "impact", "")) == "high"
        and enum_value(getattr(t, "status", "")) == "open"
    ]

    left, right = st.columns([3, 1])
    left.metric("票数（筛选后 / 全部）", f"{len(shown)} / {len(tickets)}")
    right.metric("未关闭高影响票", len(open_high))

    st.dataframe(_rows(shown, impact_label, status_label), use_container_width=True)

    st.divider()

    st.subheader("单张确认")
    open_tickets = [t for t in tickets if enum_value(getattr(t, "status", "")) == "open"]
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
                use_container_width=True,
            )
            if st.button(f"批量关闭 {len(answers)} 张"):
                closed, failures = apply_answers(store, answers)
                st.success(f"已关闭 {len(closed)} 张：" + "、".join(closed))
                for failure in failures:
                    st.warning(failure)
                st.rerun()
    return 0


def main(argv: list[str] | None = None) -> int:
    _ensure_src_on_path()
    deps = _load_deps()
    if deps is None:
        return 1
    st, Store, TicketImpact, TicketStatus, parse_answers, apply_answers = deps
    return _render(st, Store, TicketImpact, TicketStatus, parse_answers, apply_answers)


if __name__ == "__main__":
    raise SystemExit(main())

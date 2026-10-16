"""KFPA 交互层（界面）—— 質問票台账的命令行与可选 Web 页面。

分工与边界（docs/dev/p0-plan.md §1 / §3.7）：

- 本包只写 ``src/kfpa/ui/**``，不碰 ``store`` / ``pipeline`` / ``schemas``；
- 数据形状（票面五要素、影响面、状态）一律取 ``kfpa.schemas``（Chief 冻结）；
- 读写一律走 ``kfpa.store.Store``（磐石），界面自己不开 SQLite 连接；
- 高影响票挡任务包这条硬规则在 pipeline 侧判定，界面只负责把「还有几张
  未关闭高影响票」如实显示出来，保护人的确认带宽（design.md §5.1）。

命令入口：交给主入口注入 —— ``from kfpa.ui.cli import register``。
"""

from __future__ import annotations

__all__ = ["cli"]

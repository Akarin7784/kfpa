"""KFPA —— Knowledge-First Pipeline Architecture.

知识优先的流水线式多智能体软件生产架构（工程实现）。

只允许出现角色名（ARCHITECT / REVIEWER / FLASH / EMBEDDING），
模型绑定一律走 config/models.yaml —— 见 docs/dev/p0-plan.md §3.5。
"""

__version__ = "0.1.0"
CONTRACT_VERSION = "1.1"

__all__ = ["__version__", "CONTRACT_VERSION"]

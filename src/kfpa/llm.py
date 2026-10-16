"""模型路由（docs/dev/p0-plan.md §3.3）：角色名 → LLM 实例。

三类实现：
- :class:`MockLLM` —— P0 验收路径：确定性、无网络、无 key、零第三方依赖；
- :class:`LiteLLMAdapter` —— 惰性 import litellm，缺包时抛 RuntimeError（中文），绝不 ImportError 崩；
- `provider="mock"|"litellm"` 由 :func:`build_llm` 统一装配。

铁律：本文件是**唯一**允许出现模型字符串的地方（且模型名一律从 models.yaml 的角色配置里取，
不写字面量），pipeline / gates / ui 只准用角色名。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from .config import (
    DEFAULT_CONFIG_PATH,
    ROLE_NAMES,
    load_roles as _load_roles,
    normalize_role,
    role_config,
)


@runtime_checkable
class LLM(Protocol):
    """角色化补全接口（契约 §3.3）。"""

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        """按角色返回补全文本；`json_mode=True` 时返回可 `json.loads` 的字符串。"""
        ...


def load_roles(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, dict[str, Any]]:
    """契约 §3.3 的签名：委托给 :mod:`kfpa.config` 做真正的加载与校验。"""
    return _load_roles(config_path)


# ---------------------------------------------------------------- Mock

#: MockLLM 在 json_mode 下返回的固定剧本键（长流按这些键解析；改键 = 破坏契约，先在群里说）
MOCK_TICKETS_KEY = "tickets"
MOCK_PACKAGE_KEYS = ("feature", "goal", "acceptance", "boundaries",
                     "interface_contract", "prohibitions", "feedback", "closed_tickets")


class MockLLM:
    """确定性 Mock：相同输入永远相同输出，绝不联网。

    剧本选择规则（按 system+user 文本里的关键词）：
    - 出现「質問票 / 歧义 / ticket / qa」→ 質問票 JSON；
    - 出现「任务包 / 六段 / package」→ 任务包 JSON；
    - json_mode 但都没命中 → 記質問票 JSON（一轮的第一步是转译出票）。

    `high_impact=True` 时第一张票升为 high —— 供 A3「未关闭高影响票不得出任务包」构造用例，
    默认 False，保证 A2 的一条命令能跑完整轮。
    """

    name = "mock"

    TICKET_KEYWORDS = ("質問票", "歧义", "ticket", "qa")
    PACKAGE_KEYWORDS = ("任务包", "六段", "package")

    def __init__(self, roles: Optional[Mapping[str, Any]] = None, high_impact: bool = False) -> None:
        self.roles = dict(roles or {})
        self.high_impact = bool(high_impact)

    # -------------------------------------------------- 固定剧本

    def tickets(self) -> list[dict[str, str]]:
        """两张票：票面五要素齐全（票号/位置/两种解释/主BOT倾向 + 影响面）。"""
        impacts = ("high", "medium") if self.high_impact else ("medium", "low")
        return [
            {
                "ticket_id": "T-001",
                "location": "§4 轮次生命周期 ② 主BOT 转译产出質問票",
                "option_a": "每轮一次性产出全部質問票，攒批上呈人确认",
                "option_b": "按模块分批产出質問票，分批上呈",
                "architect_preference": "A —— 一次产出后攒批上呈，符合 §5.1 保护人的确认带宽",
                "impact": impacts[0],
            },
            {
                "ticket_id": "T-002",
                "location": "§5.2 任务包第 6 段 回灌区",
                "option_a": "只回灌上一轮失败原因与规避要求",
                "option_b": "累积回灌全部历史失败原因",
                "architect_preference": "A —— 只回灌上一轮，避免任务包上下文膨胀",
                "impact": impacts[1],
            },
        ]

    def task_package(self) -> dict[str, Any]:
        """六段齐全的任务包（feedback 首轮为空占位，不计缺段）。"""
        return {
            "feature": "P0 最小闭环（mock 任务包）",
            "goal": "按六段式任务包完成本轮编码任务（mock 剧本，用于离线 dry-run 验收）。",
            "acceptance": [
                {"cid": "AC1", "text": "任务包六段齐全且验收标准逐条可测"},
                {"cid": "AC2", "text": "存在未关闭高影响票时不得生成任务包（退出码非零）"},
                {"cid": "AC3", "text": "失败原因必须回灌到下一版任务包的回灌区"},
            ],
            "boundaries": [
                "输入域：原始设计文本（Markdown）非空",
                "异常流：设计文件缺失 → 中文报错，退出码非零",
                "幂等：同轮次重跑不覆盖已关闭票的人工确认结果",
            ],
            "interface_contract": [
                "run_round(round_no, design_text, design_ref, *, store, llm, executor, workdir, max_attempts=3, dry_run=True) -> RoundResult",
                "Store.save_package(package: TaskPackage) -> str（返回 package_hash）",
            ],
            "prohibitions": [
                "不许在 llm.py 之外出现模型字符串",
                "不许绕过 Store 直接写 SQLite",
                "不许引入 P0 范围外依赖（Dify / Langfuse / PR-Agent / PostgreSQL）",
            ],
            "feedback": [],
            "closed_tickets": [],
        }

    # -------------------------------------------------- 补全

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        name = normalize_role(role)
        text = f"{system}\n{user}".lower()

        if name == "EMBEDDING":
            return self._embedding(text)

        if name == "ARCHITECT":
            wants_tickets = any(k.lower() in text for k in self.TICKET_KEYWORDS)
            wants_package = any(k.lower() in text for k in self.PACKAGE_KEYWORDS)
            if wants_package and not wants_tickets:
                return self._json(self.task_package()) if json_mode else self._package_text()
            return self._json({"tickets": self.tickets()}) if json_mode else self._tickets_text()

        if name == "REVIEWER":
            review = {
                "reviews": [
                    {
                        "severity": "major",
                        "location": "任务包 段4 接口契约",
                        "issue": "接口签名与冻结契约的差异未被显式声明",
                        "suggestion": "逐条对齐 docs/dev/p0-plan.md §3 后再放行",
                    },
                    {
                        "severity": "minor",
                        "location": "任务包 段5 禁止事项",
                        "issue": "禁止事项未量化，无法门禁判定",
                        "suggestion": "改写成可被 grep / 测试断言的条目",
                    },
                ],
            }
            return self._json(review) if json_mode else self._review_text()

        # FLASH：执行结果由长流的 MockExecutor 产出；这里只给确定性占位文本。
        payload = {"ok": True, "files_changed": [], "notes": "mock 剧本：未真实改动文件"}
        return self._json(payload) if json_mode else self._flash_text()

    # -------------------------------------------------- 内部工具

    def _json(self, obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)

    def _embedding(self, text: str) -> str:
        """8 维确定性伪向量（RAG 属于 P1，P0 只保证接口形状可用）。"""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [round(b / 255.0, 6) for b in digest[:8]]
        return self._json({"embedding": vec, "dim": len(vec), "mock": True})

    def _tickets_text(self) -> str:
        return "（mock）已转译设计，产出 2 张質問票，请攒批确认后放行。"

    def _package_text(self) -> str:
        return "（mock）已按六段式生成任务包。"

    def _review_text(self) -> str:
        return "（mock）评审完成：1 条 major、1 条 minor，详见 json_mode 输出。"

    def _flash_text(self) -> str:
        return "（mock）执行完成：未真实改动文件。"


# ---------------------------------------------------------------- LiteLLM

class LiteLLMAdapter:
    """真实模型适配器（P1 起用）。惰性 import：缺包时报中文 RuntimeError，不崩。

    模型名、温度等一律从角色配置读；`thinking` 开关由 LiteLLM 代理侧配置承担，这里透传不了就不传。
    """

    name = "litellm"

    def __init__(self, roles: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        self.roles = dict(roles or {})
        self.options = dict(kwargs)

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        name = normalize_role(role)
        spec = role_config(self.roles, name)

        try:
            import litellm  # 惰性导入：P0 验收环境不需要装
        except ImportError as exc:
            raise RuntimeError(
                "未安装 litellm：P0 验收请用 --provider mock；"
                "接入真实模型需先让 Chief 统一安装（pip install litellm）"
            ) from exc

        model = spec["model"]
        if name == "EMBEDDING":
            try:
                resp = litellm.embedding(model=model, input=[user])
            except Exception as exc:
                raise RuntimeError(
                    f"模型调用失败（角色 {name}，provider=litellm）：{type(exc).__name__}: {exc}；"
                    "P0 验收请用 --provider mock"
                ) from exc
            data = resp["data"][0]["embedding"]
            return json.dumps({"embedding": list(data), "dim": len(data)}, ensure_ascii=False)

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        for key in ("temperature", "max_tokens", "top_p"):
            if key in spec:
                call_kwargs[key] = spec[key]
        if json_mode:
            call_kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = litellm.completion(**call_kwargs)
        except Exception as exc:
            # 无 key / 网络不通 / 模型名错，统一变中文 RuntimeError，CLI 不许裸崩。
            raise RuntimeError(
                f"模型调用失败（角色 {name}，provider=litellm）：{type(exc).__name__}: {exc}；"
                "P0 验收请用 --provider mock，或让 Chief 配好 key"
            ) from exc
        return resp.choices[0].message.content or ""


def build_llm(roles: Mapping[str, Any], provider: str = "mock", **kwargs: Any) -> LLM:
    """装配 LLM。`provider` 只认 mock / litellm；未知 provider 抛 ValueError（中文）。"""
    key = str(provider or "mock").strip().lower()
    if key == "mock":
        return MockLLM(roles=roles, **kwargs)
    if key == "litellm":
        return LiteLLMAdapter(roles=roles, **kwargs)
    raise ValueError(f"未知 provider：{provider!r}（可选：mock / litellm）")


__all__ = [
    "LLM",
    "MockLLM",
    "LiteLLMAdapter",
    "build_llm",
    "load_roles",
    "ROLE_NAMES",
    "MOCK_TICKETS_KEY",
    "MOCK_PACKAGE_KEYS",
]

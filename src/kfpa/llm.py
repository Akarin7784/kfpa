"""模型路由（docs/dev/p0-plan.md §3.3；Mock 剧本见 docs/dev/p0-design-addendum.md D1-7）。

三类实现：
- :class:`MockLLM` —— P0 验收路径：**不推理**，按 prompt 里的 KFPA marker 从输入文本抽机读行，
  同一输入永远同一输出，零网络零 key 零第三方依赖；
- :class:`LiteLLMAdapter` —— 惰性 import litellm，缺包/调用失败一律中文 RuntimeError，绝不 ImportError 崩；
- `provider="mock"|"litellm"` 由 :func:`build_llm` 统一装配。

铁律：本文件是**唯一**允许出现模型字符串的地方（且模型名一律从 models.yaml 的角色配置里取，
不写字面量），pipeline / gates / ui 只准用角色名。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from .config import (
    DEFAULT_CONFIG_PATH,
    ROLE_NAMES,
    load_roles as _load_roles,
    normalize_role,
    role_config,
)

# ---------------------------------------------------------------- marker（D1-7）

#: D1-7 冻结的三个 marker
MARKER_GEN_TICKETS = "[KFPA:GEN_TICKETS]"
MARKER_BUILD_PACKAGE = "[KFPA:BUILD_PACKAGE]"
MARKER_REVIEW = "[KFPA:REVIEW]"

#: marker → 允许的角色（marker 与 role 不匹配 → MockScriptError）
MARKER_ROLE: dict[str, str] = {
    MARKER_GEN_TICKETS: "ARCHITECT",
    MARKER_BUILD_PACKAGE: "ARCHITECT",
    MARKER_REVIEW: "REVIEWER",
}

#: 过渡期兼容别名（长流早期约定 KFPA_TASK=...，D1 marker 落地后应删除）
MARKER_ALIASES: dict[str, str] = {
    "kfpa_task=qa_tickets": MARKER_GEN_TICKETS,
    "kfpa_task=task_package": MARKER_BUILD_PACKAGE,
    "kfpa_task=review": MARKER_REVIEW,
}

#: 失败注入取值（D1-7）
INJECTIONS: tuple[str, ...] = ("none", "apply_fail", "test_fail")

#: 机读行前缀（D1-7）
_TICKET_PREFIX = "QA:"
_AVOID_PREFIX = "AVOID["


class MockScriptError(RuntimeError):
    """Mock 剧本错误（未知 marker / marker 与角色不匹配 / 注入值非法）——D1-7 禁止静默返回。"""


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


class MockLLM:
    """确定性 Mock（D1-7）：按 marker 抽机读行，不推理、不联网、不要 key。

    marker 与返回：
    - ``[KFPA:GEN_TICKETS]``（ARCHITECT）→ 抽 ``QA:`` 行 → **JSON 数组**（无 ticket_id，票号由 pipeline 分配）；
      无匹配行 → ``[]``；
    - ``[KFPA:BUILD_PACKAGE]``（ARCHITECT）→ 抽 ``FEATURE/GOAL/AC/BOUND/IF/NO`` 行 → 六段 JSON，
      ``feedback`` 由提示词里的 ``AVOID[`` 行填；
    - ``[KFPA:REVIEW]``（REVIEWER）→ ``{"verdict","error_category","failed_criteria","reason"}``。

    ``EMBEDDING`` 不需要 marker（RAG 属 P1），返回 8 维确定性伪向量。
    """

    name = "mock"

    def __init__(
        self,
        roles: Optional[Mapping[str, Any]] = None,
        injection: str = "none",
    ) -> None:
        self.roles = dict(roles or {})
        if injection not in INJECTIONS:
            raise MockScriptError(
                f"未知失败注入：{injection!r}（允许：{' / '.join(INJECTIONS)}）"
            )
        self.injection = injection

    # -------------------------------------------------- 补全入口

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        name = normalize_role(role)
        text = f"{system}\n{user}"

        # EMBEDDING 是 RAG 接口（P1），不参与 marker 剧本。
        if name == "EMBEDDING":
            return self._embedding(text)

        marker = self._find_marker(text)
        expected = MARKER_ROLE[marker]
        if name != expected:
            raise MockScriptError(
                f"marker {marker} 要求角色 {expected}，当前角色是 {name} → 检查提示词模板"
            )

        if marker == MARKER_GEN_TICKETS:
            return self._json(self._tickets_from_text(text))
        if marker == MARKER_BUILD_PACKAGE:
            return self._json(self._package_from_text(text))
        return self._json(self._review_from_text(text))

    # -------------------------------------------------- marker 识别

    def _find_marker(self, text: str) -> str:
        upper = text.upper()
        found: list[str] = [m for m in MARKER_ROLE if m.upper() in upper]
        lower = text.lower()
        for alias, marker in MARKER_ALIASES.items():
            if alias in lower and marker not in found:
                found.append(marker)
        if not found:
            raise MockScriptError(
                "MockLLM 未在提示词中找到 KFPA marker：需要 "
                f"{MARKER_GEN_TICKETS} / {MARKER_BUILD_PACKAGE} / {MARKER_REVIEW}"
                "（EMBEDDING 例外）→ 检查 prompt 模板是否带上 marker"
            )
        if len(found) > 1:
            raise MockScriptError(f"MockLLM 提示词里同时出现多个 marker：{found} → 一次只放一个任务")
        return found[0]

    # -------------------------------------------------- 抽机读行（D1-7）

    def _tickets_from_text(self, text: str) -> list[dict[str, str]]:
        """抽 ``QA: <位置> | A: ... | B: ... | P: ... | I: HIGH|MEDIUM|LOW`` → 票面字典数组。

        - 无 QA 行 → `[]`（D1-7 明确允许）；
        - `I:` 缺失按 MEDIUM 兜底，其余字段缺失填空串（票面完整性由 pipeline 的
          `QATicket.missing_fields()` 判定，Mock 不替它放水）；
        - impact 一律转小写，可直接喂 `TicketImpact`。
        """
        tickets: list[dict[str, str]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line.upper().startswith(_TICKET_PREFIX):
                continue
            body = line.split(":", 1)[1] if ":" in line else ""
            segments = [seg.strip() for seg in body.split("|")]
            location = segments[0] if segments else ""
            fields: dict[str, str] = {}
            for seg in segments[1:]:
                if ":" not in seg:
                    continue
                key, value = seg.split(":", 1)
                fields[key.strip().upper()] = value.strip()
            tickets.append(
                {
                    "location": location,
                    "option_a": fields.get("A", ""),
                    "option_b": fields.get("B", ""),
                    "architect_preference": fields.get("P", ""),
                    "impact": (fields.get("I") or "MEDIUM").lower(),
                }
            )
        return tickets

    def _package_from_text(self, text: str) -> dict[str, Any]:
        """抽 FEATURE/GOAL/AC/BOUND/IF/NO 行 → 六段 JSON；feedback 取 ``AVOID[`` 行。"""
        feature = ""
        goal = ""
        acceptance: list[str] = []
        boundaries: list[str] = []
        interface_contract: list[str] = []
        prohibitions: list[str] = []
        feedback: list[str] = []
        closed_tickets: list[str] = []

        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.upper().startswith(_AVOID_PREFIX):
                feedback.append(line)
                continue
            match = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", line)
            if match is None:
                continue
            key, value = match.group(1).upper(), match.group(2).strip()
            if not value:
                continue
            if key == "FEATURE":
                feature = value
            elif key == "GOAL":
                goal = value
            elif key == "AC":
                acceptance.append(value)
            elif key == "BOUND":
                boundaries.append(value)
            elif key == "IF":
                interface_contract.append(value)
            elif key == "NO":
                prohibitions.append(value)
            elif key == "CLOSED":
                closed_tickets.append(value)

        return {
            "feature": feature,
            "goal": goal,
            "acceptance": [
                {"cid": f"AC{i}", "text": item} for i, item in enumerate(acceptance, 1)
            ],
            "boundaries": boundaries,
            "interface_contract": interface_contract,
            "prohibitions": prohibitions,
            "feedback": feedback,
            "closed_tickets": closed_tickets,
        }

    def _review_from_text(self, text: str) -> dict[str, Any]:
        """评审剧本：默认 pass；`--inject apply_fail|test_fail` 时按注入返回 fail。"""
        if self.injection == "none":
            return {
                "verdict": "pass",
                "error_category": "",
                "failed_criteria": [],
                "reason": "mock 评审：未发现阻塞问题",
            }
        first_cid = ""
        for line in text.splitlines():
            if line.strip().upper().startswith("AC:"):
                first_cid = "AC1"
                break
        return {
            "verdict": "fail",
            "error_category": self.injection,
            "failed_criteria": [first_cid] if first_cid else [],
            "reason": f"mock 注入：{self.injection}",
        }

    # -------------------------------------------------- 工具

    def _json(self, obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)

    def _embedding(self, text: str) -> str:
        """8 维确定性伪向量（RAG 属 P1，P0 只保证接口形状可用）。"""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [round(b / 255.0, 6) for b in digest[:8]]
        return self._json({"embedding": vec, "dim": len(vec), "mock": True})


# ---------------------------------------------------------------- LiteLLM


class LiteLLMAdapter:
    """真实模型适配器。惰性 import：缺包 → 中文 RuntimeError（退出码 4 的语义来源）。

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
            import litellm  # 惰性导入：缺包时给中文提示而不是 ImportError 崩
        except ImportError as exc:
            raise RuntimeError(
                "未安装 litellm：这是可选依赖缺失 → 请让 Chief 统一安装（pip install litellm），"
                "或改用 --provider mock"
            ) from exc

        model = spec["model"]
        if name == "EMBEDDING":
            try:
                resp = litellm.embedding(model=model, input=[user])
            except Exception as exc:
                raise RuntimeError(
                    f"模型调用失败（角色 {name}，provider=litellm）：{type(exc).__name__}: {exc}"
                    " → P0 验收请用 --provider mock"
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
                f"模型调用失败（角色 {name}，provider=litellm）：{type(exc).__name__}: {exc}"
                " → P0 验收请用 --provider mock，或让 Chief 配好 key"
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
    "MockScriptError",
    "LiteLLMAdapter",
    "build_llm",
    "load_roles",
    "ROLE_NAMES",
    "MARKER_GEN_TICKETS",
    "MARKER_BUILD_PACKAGE",
    "MARKER_REVIEW",
    "MARKER_ROLE",
    "MARKER_ALIASES",
    "INJECTIONS",
]

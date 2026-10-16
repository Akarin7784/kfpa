"""模型路由（docs/dev/p0-plan.md §3.3；Mock 剧本见 D1-7 与 D2 裁决 R-3 / R-5 / R-10 / R-17）。

三类实现：
- :class:`MockLLM` —— P0 验收路径：**不推理**，按 prompt 里的 marker 或关键词选剧本、从输入文本抽机读行，
  同一输入永远同一输出，零网络零 key；
- :class:`LiteLLMAdapter` —— 惰性 import litellm，缺包/调用失败一律中文 RuntimeError（D2 R-7 / A9），
  绝不裸抛 AuthenticationError，也绝不 ImportError 崩；
- `provider="mock"|"litellm"` 由 :func:`build_llm` 统一装配。

剧本选择优先级（D2 R-17 / R-3）：
    1. `KFPA_TASK=qa_tickets` / `KFPA_TASK=task_package` —— **唯一规范 marker**（长流 prompt 里写的）；
    2. `[KFPA:GEN_TICKETS]` / `[KFPA:BUILD_PACKAGE]` / `[KFPA:REVIEW]` —— D1-7 的 **永久兼容别名**
       （R-17：别名永久有效、不得删除；别名与规范 marker 都必须有测试覆盖）；
    3. 关键词兜底（質問票/歧义/ticket/qa、任务包/六段/package）；
    4. 都没有 → `MockScriptError`（D1-7：禁止静默返回空串）。

铁律：模型字符串只能从 `config/models.yaml` 的角色配置里取，本文件不写任何字面量模型名。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from .config import (
    DEFAULT_CONFIG_PATH,
    PROVIDERS,
    ROLE_NAMES,
    load_roles as _load_roles,
    normalize_provider,
    normalize_role,
    resolve_provider,
    role_config,
)

# ---------------------------------------------------------------- marker

#: D2 R-17：唯一规范 marker（k=v 形式，便于加参数与 grep）
MARKER_TASK_QA = "KFPA_TASK=qa_tickets"
MARKER_TASK_PACKAGE = "KFPA_TASK=task_package"

#: D1-7 的 marker —— **永久兼容别名，不得删除**（R-17）
MARKER_GEN_TICKETS = "[KFPA:GEN_TICKETS]"
MARKER_BUILD_PACKAGE = "[KFPA:BUILD_PACKAGE]"
MARKER_REVIEW = "[KFPA:REVIEW]"

#: 归一化后的剧本名
SCRIPT_TICKETS = "tickets"
SCRIPT_PACKAGE = "package"
SCRIPT_REVIEW = "review"

#: 识别输入 → (剧本名, 允许角色)
_MARKER_TABLE: tuple[tuple[str, str, str], ...] = (
    (MARKER_TASK_QA, SCRIPT_TICKETS, "ARCHITECT"),
    (MARKER_TASK_PACKAGE, SCRIPT_PACKAGE, "ARCHITECT"),
    (MARKER_GEN_TICKETS, SCRIPT_TICKETS, "ARCHITECT"),
    (MARKER_BUILD_PACKAGE, SCRIPT_PACKAGE, "ARCHITECT"),
    (MARKER_REVIEW, SCRIPT_REVIEW, "REVIEWER"),
)

#: 关键词兜底（D2 R-3）
_TICKET_KEYWORDS = ("質問票", "歧义", "ticket", "qa")
_PACKAGE_KEYWORDS = ("任务包", "六段", "package")

#: D2 R-10：失败注入值域
INJECTIONS: tuple[str, ...] = ("none", "high_ticket", "apply_fail", "test_fail")

#: D1-7 机读行前缀
_TICKET_PREFIX = "QA:"
_AVOID_PREFIX = "AVOID["


class MockScriptError(RuntimeError):
    """Mock 剧本错误（未知 marker / marker 与角色不匹配 / 无 marker 且无关键词 / 注入值非法）。"""


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
    """确定性 Mock（D1-7 / D2 R-3）：抽机读行，不推理、不联网、不要 key。

    返回结构（D2 R-3）：
    - 質問票：``{"tickets":[{location,option_a,option_b,architect_preference,impact}...]}``
      —— **不带 `ticket_id`**，票号由 pipeline 按 `R{round}-{seq:03d}` 分配（R-3 的 duplicate keyword 防护）；
      无 QA 行 → ``{"tickets":[]}``；
    - 任务包：``{feature,goal,acceptance:[{cid,text}],boundaries,interface_contract,prohibitions,
      feedback,closed_tickets}``，`feedback` 由提示词里的 ``AVOID[`` 行填；
    - 评审（D2 R-5）：``{"reviews":[{severity,location,issue,suggestion}...]}``，不参与 P0 门禁。

    `high_impact=True` 或 `injection="high_ticket"` 时首张票升为 HIGH（D2 R-10 的 A3 注入件）；
    默认两张票 impact = medium/low，保证 A2 一条命令跑完整轮。
    """

    name = "mock"

    def __init__(
        self,
        roles: Optional[Mapping[str, Any]] = None,
        injection: str = "none",
        high_impact: bool = False,
    ) -> None:
        self.roles = dict(roles or {})
        if injection not in INJECTIONS:
            raise MockScriptError(
                f"未知失败注入：{injection!r}（允许：{' / '.join(INJECTIONS)}）"
            )
        self.injection = injection
        # R-10：high_ticket 只影响 mock 剧本；两种接线口径都支持
        self.high_impact = bool(high_impact or injection == "high_ticket")

    # -------------------------------------------------- 补全入口

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        name = normalize_role(role)
        text = f"{system}\n{user}"

        # EMBEDDING 是 RAG 接口（P1），不参与 marker 剧本。
        if name == "EMBEDDING":
            return self._embedding(text)

        script, expected = self._select_script(text)
        if name != expected:
            raise MockScriptError(
                f"marker 对应的剧本是 {script}，要求角色 {expected}，当前角色是 {name} → 检查提示词模板"
            )

        if script == SCRIPT_TICKETS:
            return self._json({"tickets": self._tickets_from_text(text)})
        if script == SCRIPT_PACKAGE:
            return self._json(self._package_from_text(text))
        return self._json(self._review_from_text(text))

    # -------------------------------------------------- 剧本选择（D2 R-3）

    def _select_script(self, text: str) -> tuple[str, str]:
        """marker 优先，关键词兜底；未知的 `[KFPA:` marker 仍然报错（D1-7）。"""
        upper = text.upper()
        for marker, script, role in _MARKER_TABLE:
            if marker.upper() in upper:
                return script, role

        # 出现了 KFPA 风格的 marker 但不认识 → 报错，别猜（D1-7）
        if "[KFPA:" in upper or "KFPA_TASK=" in upper:
            known = " / ".join(m for m, _, _ in _MARKER_TABLE)
            raise MockScriptError(f"未知 KFPA marker → 检查提示词模板（已支持：{known}）")

        lower = text.lower()
        if any(k.lower() in lower for k in _TICKET_KEYWORDS):
            return SCRIPT_TICKETS, "ARCHITECT"
        if any(k.lower() in lower for k in _PACKAGE_KEYWORDS):
            return SCRIPT_PACKAGE, "ARCHITECT"
        raise MockScriptError(
            "MockLLM 既没有 marker 也没有可选的关键词 → 检查提示词模板"
            f"（marker：{MARKER_TASK_QA} / {MARKER_TASK_PACKAGE}）"
        )

    # -------------------------------------------------- 抽机读行（D1-7）

    def _tickets_from_text(self, text: str) -> list[dict[str, str]]:
        """抽 ``QA: <位置> | A: ... | B: ... | P: ... | I: HIGH|MEDIUM|LOW`` → 票面字典数组。

        - 无 QA 行 → `[]`（D1-7 明确允许）；
        - `I:` 缺失按 MEDIUM 兜底，其余字段缺失填空串（票面完整性由 pipeline 的
          `QATicket.missing_fields()` 判定，Mock 不替它放水）；
        - impact 一律转小写，可直接喂 `TicketImpact`；
        - **不带 `ticket_id`**（D2 R-3：票号由 pipeline 分配，避免 `QATicket(**raw)` duplicate keyword）。
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

        if self.high_impact and tickets:
            tickets[0]["impact"] = "high"
        return tickets

    def _package_from_text(self, text: str) -> dict[str, Any]:
        """抽 FEATURE/GOAL/AC/BOUND/IF/NO 行 → 六段 JSON；feedback 取 ``AVOID[`` 行（D1-11）。"""
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
        """D2 R-5：返回 `{"reviews":[{severity,location,issue,suggestion}...]}`，不决定三态。"""
        reviews = [
            {
                "severity": "major",
                "location": "任务包 段4 接口契约",
                "issue": "接口签名与冻结契约的差异未显式声明",
                "suggestion": "逐条对齐 docs/dev/p0-plan.md §3 后再放行",
            },
            {
                "severity": "minor",
                "location": "任务包 段5 禁止事项",
                "issue": "禁止事项未量化，无法门禁判定",
                "suggestion": "改写成可被 grep / 测试断言的条目",
            },
        ]
        if self.injection in ("apply_fail", "test_fail"):
            reviews.append(
                {
                    "severity": "major",
                    "location": "执行器",
                    "issue": f"mock 注入：{self.injection}",
                    "suggestion": "看门禁 FAILING 项与回灌区",
                }
            )
        return {"reviews": reviews}

    # -------------------------------------------------- 工具

    def _json(self, obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)

    def _embedding(self, text: str) -> str:
        """8 维确定性伪向量（RAG 属 P1，P0 只保证接口形状可用）。"""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [round(b / 255.0, 6) for b in digest[:8]]
        return self._json({"embedding": vec, "dim": len(vec), "mock": True})


# ---------------------------------------------------------------- MockHetero（P2）


#: `MockHeteroLLM` 的确定性注入档位（对应 G8 的每一条折算分支）
HETERO_INJECTIONS: tuple[str, ...] = (
    "none",                      # 按提示词里的 DEFECT: 行判定
    "major_with_criterion",      # major + criterion → G8 阻塞
    "major_without_criterion",   # major 无 criterion → 只记 reasons
    "minor",                     # 只记 reasons
    "info",                      # 只记 reasons
    "malformed",                 # 非 JSON → 解析失败只记 reasons（R-5）
)

#: 提示词里的机读缺陷行：`DEFECT: AC2 | location: 段4 接口契约 | kind: missing_error_code`
_DEFECT_PREFIX = "DEFECT:"


class MockHeteroLLM:
    """provider="mock_hetero"（P2 D4-3.2）：**确定性异构评审剧本**，离线可验收。

    与 `MockLLM` 的区别：它扮演「另一家模型」的评审位，只服务 REVIEWER，
    输出 D4-3.1 的 `ReviewReport` 形状（`verdict` + `findings[{severity,location,issue,suggestion,criterion}]`）。

    判定规则（不推理，只看机读行）：
    - 提示词里出现 ``DEFECT: <criterion>`` 行 → 一条 `major` + 该 criterion（verdict=fail）；
    - 没有 DEFECT 行 → verdict=pass、findings 为空；
    - 构造函数 `inject` 可强制某个 G8 分支（供 A18/A22 逐条验折算），取值见 :data:`HETERO_INJECTIONS`。

    `REVIEWER` 之外的角色一律 `MockScriptError`（异构位只做评审，不抢 ARCHITECT/FLASH 的活）。
    """

    name = "mock_hetero"

    def __init__(
        self,
        roles: Optional[Mapping[str, Any]] = None,
        inject: str = "none",
        **kwargs: Any,
    ) -> None:
        self.roles = dict(roles or {})
        if inject not in HETERO_INJECTIONS:
            raise MockScriptError(
                f"未知 mock_hetero 注入档：{inject!r}（允许：{' / '.join(HETERO_INJECTIONS)}）"
            )
        self.inject = inject
        self.options = dict(kwargs)

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        name = normalize_role(role)
        if name != "REVIEWER":
            raise MockScriptError(
                f"mock_hetero 只服务 REVIEWER（异构评审位），当前角色是 {name} → 检查 provider 配置"
            )
        if self.inject == "malformed":
            return "（mock_hetero）这不是 JSON：模拟评审格式不合法，门禁只应记 reasons"
        findings = self._findings_from_text(f"{system}\n{user}")
        self._apply_injection(findings)
        verdict = "fail" if any(f["severity"] == "major" for f in findings) else "pass"
        return json.dumps({"verdict": verdict, "findings": findings}, ensure_ascii=False, sort_keys=True)

    # -------------------------------------------------- 内部

    def _findings_from_text(self, text: str) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line.upper().startswith(_DEFECT_PREFIX):
                continue
            body = line.split(":", 1)[1]
            segments = [seg.strip() for seg in body.split("|")]
            criterion = segments[0] if segments else ""
            fields: dict[str, str] = {}
            for seg in segments[1:]:
                if ":" in seg:
                    key, value = seg.split(":", 1)
                    fields[key.strip().lower()] = value.strip()
            findings.append(
                {
                    "severity": "major",
                    "location": fields.get("location", "任务包"),
                    "issue": fields.get("kind", "异构评审发现的问题"),
                    "suggestion": fields.get("suggestion", "按 criterion 修正后重跑"),
                    "criterion": criterion,
                }
            )
        return findings

    def _apply_injection(self, findings: list[dict[str, str]]) -> None:
        if self.inject == "none":
            return
        base = {"location": "任务包", "issue": f"mock_hetero 注入：{self.inject}", "suggestion": "见 G8 折算"}
        if self.inject == "major_with_criterion":
            findings.append({**base, "severity": "major", "criterion": "AC1"})
        elif self.inject == "major_without_criterion":
            findings.append({**base, "severity": "major", "criterion": ""})
        elif self.inject == "minor":
            findings.append({**base, "severity": "minor", "criterion": ""})
        elif self.inject == "info":
            findings.append({**base, "severity": "info", "criterion": ""})


class RoleRoutingLLM:
    """按角色解析 provider 的路由器（P2 D4-3.2）。

    `resolve_provider()` 决定每个角色用哪个适配器；同一个 provider 的适配器只构造一次。
    """

    name = "role-router"

    def __init__(
        self,
        roles: Mapping[str, Any],
        override: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self.roles = dict(roles or {})
        self.override = override
        self.options = dict(kwargs)
        self._adapters: dict[str, LLM] = {}

    def provider_for(self, role: str) -> str:
        return resolve_provider(self.roles, role, self.override)

    def adapter_for(self, role: str) -> LLM:
        provider = self.provider_for(role)
        if provider not in self._adapters:
            self._adapters[provider] = _build_single(self.roles, provider, **self.options)
        return self._adapters[provider]

    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str:
        return self.adapter_for(role).complete(role, system, user, json_mode=json_mode)


def _build_single(roles: Mapping[str, Any], provider: str, **kwargs: Any) -> LLM:
    """构造单个 provider 的适配器（未知 provider → ValueError 中文）。"""
    key = normalize_provider(provider)
    if key == "mock":
        return MockLLM(roles=roles, **kwargs)
    if key == "mock_hetero":
        return MockHeteroLLM(roles=roles, **kwargs)
    return LiteLLMAdapter(roles=roles, **kwargs)


# ---------------------------------------------------------------- LiteLLM


class LiteLLMAdapter:
    """真实模型适配器（D2 R-7 / A9）：一切调用异常统一包成中文 `RuntimeError`。

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
            # A9：无 key / 网络不通 / 模型名错，一律中文 RuntimeError 且提示 mock 路径。
            raise RuntimeError(
                f"模型调用失败（角色 {name}，provider=litellm）：{type(exc).__name__}: {exc}"
                " → P0 验收请用 --provider mock，或让 Chief 配好 key"
            ) from exc
        return resp.choices[0].message.content or ""


def _build_single(roles: Mapping[str, Any], provider: str, **kwargs: Any) -> LLM:
    """构造单个 provider 的适配器（未知 provider → ValueError 中文）。

    每个适配器只吃自己的参数（`mock` 吃 injection/high_impact、`mock_hetero` 吃 inject），
    其余一律丢掉——路由器的 kwargs 是全局的，不能因为给 REVIEWER 的档位把 ARCHITECT 顶炸。
    """
    key = normalize_provider(provider)
    if key == "mock":
        allowed = {k: v for k, v in kwargs.items() if k in ("injection", "high_impact")}
        return MockLLM(roles=roles, **allowed)
    if key == "mock_hetero":
        allowed = {k: v for k, v in kwargs.items() if k == "inject"}
        return MockHeteroLLM(roles=roles, **allowed)
    if key == "litellm":
        return LiteLLMAdapter(roles=roles)
    raise ValueError(f"未知 provider：{provider!r}（可选：{' / '.join(PROVIDERS)}）")


def build_llm(
    roles: Mapping[str, Any],
    provider: Optional[str] = None,
    **kwargs: Any,
) -> LLM:
    """装配 LLM（P2 D4-3.2）。

    - `provider=None`（默认）→ :class:`RoleRoutingLLM`：**按角色**解析 provider
      （CLI 全局 `--provider` > 角色 `provider:` > `mock`）；
    - `provider="mock" | "mock_hetero" | "litellm"` → 对所有角色**强制**该 provider（旧行为兼容）；
    - 未知 provider → `ValueError`（中文）。
    """
    if provider is None or not str(provider).strip():
        return RoleRoutingLLM(roles, override=None, **kwargs)
    key = normalize_provider(provider)
    return _build_single(roles, key, **kwargs)


__all__ = [
    "LLM",
    "MockLLM",
    "MockHeteroLLM",
    "RoleRoutingLLM",
    "MockScriptError",
    "LiteLLMAdapter",
    "build_llm",
    "load_roles",
    "ROLE_NAMES",
    "PROVIDERS",
    "HETERO_INJECTIONS",
    "MARKER_TASK_QA",
    "MARKER_TASK_PACKAGE",
    "MARKER_GEN_TICKETS",
    "MARKER_BUILD_PACKAGE",
    "MARKER_REVIEW",
    "SCRIPT_TICKETS",
    "SCRIPT_PACKAGE",
    "SCRIPT_REVIEW",
    "INJECTIONS",
]

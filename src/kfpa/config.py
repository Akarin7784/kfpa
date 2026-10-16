"""角色配置加载与校验（docs/dev/p0-plan.md §2 / §3.3）。

铁律：**模型字符串只允许出现在 `config/models.yaml` 与 `llm.py` 的适配器内部**。
本模块只负责「把 yaml 读成结构、把角色名校验干净」，绝不硬编码任何模型名，
也绝不把模型名往下游（pipeline / gates / ui）泄漏——下游只准用角色名。

角色集合（与 schemas.py 的 CONTRACT_VERSION 1.0 一致）：
    ARCHITECT / REVIEWER / FLASH / EMBEDDING
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

#: 全部合法角色名（唯一权威列表，其它模块请从这里 import）
ROLE_NAMES: tuple[str, ...] = ("ARCHITECT", "REVIEWER", "FLASH", "EMBEDDING")

#: P0 最小闭环真正用到的角色：缺一个就直接报错（别的角色缺失可以容错）
CORE_ROLES: tuple[str, ...] = ("ARCHITECT", "REVIEWER", "FLASH")

#: 默认配置路径（相对仓库根目录）
DEFAULT_CONFIG_PATH = "config/models.yaml"

#: 可覆盖配置路径的环境变量（R-26）
CONFIG_ENV = "KFPA_CONFIG"

#: 合法 provider（P2 D4-3.2）：角色级 `provider:` 字段与 CLI `--provider` 共用白名单
PROVIDERS: tuple[str, ...] = ("mock", "mock_hetero", "litellm")

#: 默认 provider（角色没写 `provider:` 时）
DEFAULT_PROVIDER = "mock"


def resolve_config_path(config_path: str | Path = DEFAULT_CONFIG_PATH) -> Path:
    """R-26：把配置路径解析成一个真实文件，**不依赖 cwd**。

    顺序：`--config` 显式（给了就只认它，不做回退）→ `KFPA_CONFIG` →
    cwd 及其父目录下的 `config/models.yaml` → 包/仓库安装位置（`kfpa` 包上溯）。
    都没有 → 中文列出已试过的路径（不猜、不回退到空配置）。
    """
    explicit = str(config_path or "").strip()
    if explicit and explicit != DEFAULT_CONFIG_PATH:
        path = Path(explicit)
        if path.is_file():
            return path
        raise FileNotFoundError(f"角色配置不存在：{path}（--config 显式指定，R-26 不做回退）")

    candidates: list[Path] = []
    env_path = os.environ.get(CONFIG_ENV, "").strip()
    if env_path:
        candidates.append(Path(env_path))
    here = Path.cwd().resolve()
    candidates.extend(base / DEFAULT_CONFIG_PATH for base in (here, *here.parents))
    package_root = Path(__file__).resolve()
    candidates.extend(base / DEFAULT_CONFIG_PATH for base in package_root.parents)

    tried: list[str] = []
    for candidate in candidates:
        key = str(candidate)
        if key in tried:
            continue
        tried.append(key)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "角色配置不存在（R-26：依次试过 --config / KFPA_CONFIG / cwd 及父目录 / 包位置）：\n  - "
        + "\n  - ".join(tried)
    )


def normalize_role(role: str) -> str:
    """把角色名规范成大写并校验；未知角色抛 ValueError（中文提示）。"""
    name = str(role or "").strip().upper()
    if name not in ROLE_NAMES:
        allowed = " / ".join(ROLE_NAMES)
        raise ValueError(f"未知角色：{role!r}（允许的角色：{allowed}）")
    return name


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """读取 models.yaml（只做语法层校验），返回整个配置字典。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境问题，非逻辑分支
        raise RuntimeError(
            "缺少 pyyaml：请让 Chief 统一安装（pip install pyyaml），不要在项目目录里自建 venv"
        ) from exc

    path = resolve_config_path(config_path)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"角色配置解析失败：{path}：{exc}") from exc

    if not isinstance(data, Mapping):
        raise ValueError(f"角色配置格式错误：{path} 顶层必须是映射（收到 {type(data).__name__}）")
    return dict(data)


def normalize_provider(provider: str | None) -> str:
    """规范并校验 provider 名；未知值抛 ValueError（中文）。None/空 → 默认 `mock`。"""
    name = str(provider or "").strip().lower() or DEFAULT_PROVIDER
    if name not in PROVIDERS:
        raise ValueError(f"未知 provider：{provider!r}（允许：{' / '.join(PROVIDERS)}）")
    return name


def resolve_provider(
    roles: Mapping[str, Any] | None,
    role: str,
    override: str | None = None,
) -> str:
    """按角色解析 provider（P2 D4-3.2）。

    优先级：**CLI 全局 `--provider`（override）> 角色自己的 `provider:` > 默认 `mock`**。
    `override=None` / 空串 = 不覆盖（走角色配置）。
    """
    if override is not None and str(override).strip():
        return normalize_provider(override)
    name = normalize_role(role)
    spec = roles.get(name) if isinstance(roles, Mapping) else None
    configured = spec.get("provider") if isinstance(spec, Mapping) else None
    return normalize_provider(configured)


def validate_roles(roles: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """校验角色表并返回规范化副本。

    规则（可被镜鉴逐条攻击）：
    1. `roles` 必须是非空映射；
    2. 角色名必须在 :data:`ROLE_NAMES` 内——多出未知角色 = 配置错误，直接报错（防拼写错误静默降级）；
    3. :data:`CORE_ROLES` 必须齐全，且每项是映射、`model` 是非空字符串；
    4. EMBEDDING 不参与 P0 验收，存在就必须合法，缺失不算错；
    5. **P2**：角色若写了 `provider:`，必须在 :data:`PROVIDERS` 白名单内（写错就报错，不静默降级成 mock）。
    """
    if not isinstance(roles, Mapping) or not roles:
        raise ValueError("角色配置为空：models.yaml 里的 roles 必须是非空映射")

    result: dict[str, dict[str, Any]] = {}
    for raw_name, spec in roles.items():
        name = normalize_role(str(raw_name))
        if not isinstance(spec, Mapping):
            raise ValueError(f"角色 {name} 的配置必须是映射（收到 {type(spec).__name__}）")
        model = spec.get("model")
        if model is None or not str(model).strip():
            raise ValueError(f"角色 {name} 缺少非空的 model 字段（模型绑定只能在 models.yaml 里改）")
        if spec.get("provider") is not None:
            normalize_provider(spec.get("provider"))  # 未知 provider → ValueError
        result[name] = dict(spec)

    missing = [name for name in CORE_ROLES if name not in result]
    if missing:
        raise ValueError(f"角色配置缺少 P0 必需角色：{' / '.join(missing)}（允许缺失：EMBEDDING）")
    return result


def load_roles(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, dict[str, Any]]:
    """加载 + 校验，返回 `{角色名: 角色配置}`。下游只读这里的键。"""
    return validate_roles(load_config(config_path).get("roles"))


def role_config(roles: Mapping[str, Any], role: str) -> dict[str, Any]:
    """取单个角色配置；未绑定时抛 RuntimeError（中文提示）。"""
    name = normalize_role(role)
    spec = roles.get(name) if isinstance(roles, Mapping) else None
    if not isinstance(spec, Mapping) or not str(spec.get("model") or "").strip():
        raise RuntimeError(f"角色 {name} 未在 config/models.yaml 中绑定 model")
    return dict(spec)


__all__ = [
    "ROLE_NAMES",
    "CORE_ROLES",
    "DEFAULT_CONFIG_PATH",
    "CONFIG_ENV",
    "PROVIDERS",
    "DEFAULT_PROVIDER",
    "normalize_role",
    "normalize_provider",
    "resolve_provider",
    "load_config",
    "validate_roles",
    "load_roles",
    "role_config",
    "resolve_config_path",
]

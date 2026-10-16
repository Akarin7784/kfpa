"""A9 验收命令（`p0-plan §4/A9`）：litellm 调用异常必须统一包成中文 RuntimeError（R-7）。

R-7 的三条：
①抛 `RuntimeError`（不是裸抛底层 `AuthenticationError` / `ValueError` 之类）；
②消息含中文提示与 `--provider mock`；③不产生网络长阻塞（本文件 monkeypatch 掉底层调用）。

本机已装 litellm，所以不再测「缺包报错」那条（R-7 已作废该测法）。
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _llm_mod():
    try:
        return importlib.import_module("kfpa.llm")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"kfpa.llm 未交付：{type(exc).__name__}: {exc}")


def _fake_adapter(m):
    roles = m.load_roles()
    for spec in roles.values():
        if isinstance(spec, dict):
            spec["model"] = "nonexistent/fake-model-for-a9"   # 假模型名 + 空 key
    try:
        return m.build_llm(roles, provider="litellm", api_key="")
    except Exception:  # noqa: BLE001
        try:
            return m.LiteLLMAdapter(roles, api_key="")
        except TypeError:
            return m.LiteLLMAdapter(roles)


def test_litellm_exception_wrapped_as_chinese_runtime_error(monkeypatch):
    pytest.importorskip("litellm", reason="本机未装 litellm，R-7 的真实调用跳过")
    import litellm

    m = _llm_mod()
    if not hasattr(m, "LiteLLMAdapter"):
        pytest.fail("R-7：缺 LiteLLMAdapter（§3.3）")
    adapter = _fake_adapter(m)

    def boom(*args, **kwargs):
        raise ValueError("simulated-network-boom-a9")

    # 拦截一切可能的底层调用点，既证明包装逻辑、又避免真实网络
    patched = []
    for name in ("completion", "acompletion", "text_completion"):
        if hasattr(litellm, name):
            monkeypatch.setattr(litellm, name, boom)
            patched.append(f"litellm.{name}")
    amod = sys.modules.get(type(adapter).__module__)
    for name in ("completion", "acompletion"):
        if amod is not None and hasattr(amod, name):
            monkeypatch.setattr(amod, name, boom)
            patched.append(f"{amod.__name__}.{name}")
    assert patched, ("R-7：无法拦截任何底层调用点，包装逻辑无法断言；"
                     "建议把 litellm 调用收敛到一个可注入的方法上")

    with pytest.raises(RuntimeError) as excinfo:
        adapter.complete("FLASH", "sys", "usr")

    message = str(excinfo.value)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in message), \
        f"R-7②：包装后的异常消息没有中文提示：{message!r}"
    assert "--provider" in message and "mock" in message, \
        f"R-7②：消息未提示 `--provider mock`：{message!r}"


def test_mock_adapter_is_deterministic_and_role_guarded():
    m = _llm_mod()
    if not hasattr(m, "MockLLM"):
        pytest.fail("§3.3：缺 MockLLM（离线验收路径）")
    llm = m.build_llm(m.load_roles(), provider="mock")
    prompt = ("KFPA_TASK=qa_tickets\n"
              "QA: design.md §5.1 | A: 甲 | B: 乙 | P: 甲 | I: MEDIUM")
    first = llm.complete("ARCHITECT", "sys", prompt)
    second = llm.complete("ARCHITECT", "sys", prompt)
    assert isinstance(first, str) and first == second, "D1-7：MockLLM 必须确定性"

    with pytest.raises(Exception):
        m.MockLLM().complete("BOGUS", "sys", prompt)


def test_both_marker_forms_are_supported_and_equivalent():
    """R-17：`KFPA_TASK=` 是规范 marker，`[KFPA:…]` 是永久兼容别名，两者必须等价。"""
    m = _llm_mod()
    llm = m.build_llm(m.load_roles(), provider="mock")
    qa = "QA: design.md §5.1 | A: 甲 | B: 乙 | P: 甲 | I: MEDIUM"
    canonical = llm.complete("ARCHITECT", "sys", f"KFPA_TASK=qa_tickets\n{qa}")
    alias = llm.complete("ARCHITECT", "sys", f"[KFPA:GEN_TICKETS]\n{qa}")
    assert canonical == alias, "R-17：规范 marker 与兼容别名输出不一致"

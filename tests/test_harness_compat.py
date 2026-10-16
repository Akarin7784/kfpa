"""台架自身的兼容性单测。

P1（D3 §3.2）会把 `kfpa.store.Store` 改画成 Protocol、并新增 `make_store(dsn)` 工厂；
而 P0 门禁 `scripts/verify_p0.py` 是需要长期存活的固定门禁。这里把「两种工厂形态都能用」
的兼容行为钉死，避免有人改回去（或者将来再发一次 `replace_all` 误伤兜底行）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_verify_p0():
    spec = importlib.util.spec_from_file_location("kfpa_verify_p0", ROOT / "scripts/verify_p0.py")
    mod = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules：verify_p0.py 用了 `from __future__ import annotations`，
    # dataclass 处理字符串注解时会按 `cls.__module__` 回查 sys.modules，缺登记就会 AttributeError。
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_verify_p0_prefers_make_store_when_available():
    mod = _load_verify_p0()
    sentinel = object()
    stub = types.SimpleNamespace(make_store=lambda dsn: sentinel,
                                 Store=lambda path: "P0-fallback")
    assert mod.make_store_for(stub, "ignored") is sentinel, \
        "有 make_store() 时必须优先用它（P1 的 Store 是 Protocol，直接实例化会炸）"


def test_verify_p0_falls_back_to_store_constructor():
    mod = _load_verify_p0()
    stub = types.SimpleNamespace(Store=lambda path: ("P0", path))
    assert mod.make_store_for(stub, "x.db") == ("P0", "x.db")


def test_verify_p0_helper_does_not_recurse():
    """兜底行曾经被 replace_all 换成调用自身 —— 用 make_store 抛异常来验证回退路径不递归。"""
    mod = _load_verify_p0()

    def boom(_dsn):
        raise RuntimeError("p1 store factory failed")

    stub = types.SimpleNamespace(make_store=boom, Store=lambda path: ("P0", path))
    assert mod.make_store_for(stub, "y.db") == ("P0", "y.db")


def test_verify_p1_stdio_guard_exists():
    """R-19：P1 台架也必须自带 stdio 兜底（P1 台架未交付时跳过）。"""
    p1 = ROOT / "scripts/verify_p1.py"
    if not p1.exists():
        import pytest
        pytest.skip("p1/verify 分支尚未合并，verify_p1.py 不存在")
    text = p1.read_text(encoding="utf-8")
    assert "_force_utf8_stdio" in text and "reconfigure(encoding=\"utf-8\"" in text

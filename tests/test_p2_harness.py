"""P2 台架自身的单测：覆盖率白名单口径必须按 D4 §3.5 算对（这是 A20 的判据基础）。

R-27 的教训：台架的判定函数最容易「修完这次、下次又踩」，一律配单测。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_verify_p2():
    spec = importlib.util.spec_from_file_location("kfpa_verify_p2", ROOT / "scripts/verify_p2.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _cov(files: dict) -> dict:
    return {"files": {p: {"summary": s} for p, s in files.items()}}


def test_cover_rate_only_counts_whitelist():
    mod = _load_verify_p2()
    cov = _cov({
        "src/kfpa/schemas.py": {"covered_lines": 40, "num_statements": 50},
        "src/kfpa/ui/streamlit_app.py": {"covered_lines": 0, "num_statements": 200},
        "scripts/verify_p0.py": {"covered_lines": 0, "num_statements": 300},
        "src/kfpa/executor.py": {"covered_lines": 0, "num_statements": 100},
    })
    rate, picked, stmts = mod.cover_rate(cov)
    assert picked == ["src/kfpa/schemas.py"], picked
    assert stmts == 50
    assert abs(rate - 0.8) < 1e-9


def test_cover_rate_includes_store_package_and_p2_modules():
    mod = _load_verify_p2()
    cov = _cov({
        "src/kfpa/store/sqlite.py": {"covered_lines": 90, "num_statements": 100},
        "src/kfpa/review.py": {"covered_lines": 45, "num_statements": 50},
        "src/kfpa/eval.py": {"covered_lines": 5, "num_statements": 50},
    })
    rate, picked, stmts = mod.cover_rate(cov)
    assert sorted(picked) == ["src/kfpa/eval.py", "src/kfpa/review.py", "src/kfpa/store/sqlite.py"]
    assert stmts == 200
    assert abs(rate - 140 / 200) < 1e-9


def test_cover_rate_handles_windows_paths_and_empty_input():
    mod = _load_verify_p2()
    cov = _cov({"src\\kfpa\\gates.py": {"covered_lines": 8, "num_statements": 10}})
    rate, picked, _ = mod.cover_rate(cov)
    assert picked == ["src/kfpa/gates.py"]
    assert abs(rate - 0.8) < 1e-9
    assert mod.cover_rate({})[0] == 0.0
    assert mod.cover_rate({"files": {}})[2] == 0


def test_normalize_rows_matches_r27_contract():
    mod = _load_verify_p2()
    n = mod.normalize_rows
    assert n([("a",), ("b",)]) == ["a", "b"]
    assert n(["a", "b"]) == ["a", "b"]
    assert n([("a",), "b", None]) == ["a", "b"]
    assert n([("ARCHITECT", 2)]) == [["ARCHITECT", 2]]

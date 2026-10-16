"""R-27：A12 探针的驱动归一函数必须有单测（喂元组 / 标量 / 混合三种输入）。

背景：A12 走原始 SQL 比对两库，SQLite 的行是标量、psycopg3 的行是元组；
不归一就会出「票号一致 SQLite=['R1-001'] PG=[['R1-001']]」这种假红（R-27 实测）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_verify_p1():
    spec = importlib.util.spec_from_file_location("kfpa_verify_p1", ROOT / "scripts/verify_p1.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclass + `from __future__ import annotations`：必须先登记 sys.modules
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_pg_tuple_rows_become_scalars():
    n = _load_verify_p1().normalize_rows
    assert n([("R1-001",), ("R1-002",)]) == ["R1-001", "R1-002"]


def test_sqlite_scalar_rows_stay_scalars():
    n = _load_verify_p1().normalize_rows
    assert n(["R1-001", "R1-002"]) == ["R1-001", "R1-002"]


def test_mixed_and_none_rows():
    n = _load_verify_p1().normalize_rows
    assert n([("R1-001",), "R1-002", None, ("R1-003",)]) == ["R1-001", "R1-002", "R1-003"]
    assert n(None) == []
    assert n([]) == []


def test_multi_column_rows_keep_list_shape():
    n = _load_verify_p1().normalize_rows
    assert n([("ARCHITECT", 2)]) == [["ARCHITECT", 2]]
    assert n([[1, "pass", "kfpa-r1"]]) == [[1, "pass", "kfpa-r1"]]


def test_both_drivers_compare_equal_after_normalization():
    """同一批数据的两种驱动行形状，归一后必须相等（这就是 A12 该比的东西）。"""
    n = _load_verify_p1().normalize_rows
    pg_rows = [("R5356-001",), ("R5356-002",)]
    sq_rows = ["R5356-001", "R5356-002"]
    assert sorted(n(pg_rows)) == sorted(n(sq_rows))

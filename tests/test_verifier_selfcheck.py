"""台架自检：验收工具本身必须先被证伪一遍，否则它只是一张会变绿的纸。

这几条测试用「故意残缺/故意投毒」的桩仓库跑 `scripts/verify_p0.py`，确认：
- 模块缺失时报 FAIL 而不是 PASS/SKIP（防空绿）；
- 模型字符串铁律能被真的抓出来（防漏检）；
- 白名单内的适配器不被误杀（防假红）；
- 契约版本漂移会被点名（防静默兼容）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "verify_p0.py"


def _write_stub(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _run(root: Path, *args: str):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(RUNNER), "--root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=300,
    )


def _report(tmp_path: Path, root: Path, *args: str) -> dict:
    out = tmp_path / "evidence.json"
    cp = _run(root, *args, "--json", str(out))
    assert out.exists(), f"台架未产出 JSON 证据：stdout={cp.stdout!r} stderr={cp.stderr!r}"
    return json.loads(out.read_text(encoding="utf-8"))


def _by_id(report: dict, cid: str) -> dict:
    for c in report["checks"]:
        if c["cid"] == cid:
            return c
    raise AssertionError(f"报告里没有断言 {cid}：{[c['cid'] for c in report['checks']]}")


def _findings(check: dict) -> str:
    parts = list(check["findings"])
    for e in check["evidence"]:
        parts += [e.get("cmd", ""), e.get("note", ""), e.get("stdout", ""), e.get("stderr", "")]
    return "\n".join(parts)


def test_unknown_check_id_is_a_usage_error():
    cp = _run(ROOT, "--only", "NOPE")
    assert cp.returncode == 2, cp.stdout + cp.stderr


def test_runner_lists_a1_to_a7():
    cp = _run(ROOT, "--list")
    assert cp.returncode == 0
    for cid in ("A1", "A2", "A3", "A4", "A5.1", "A5.2", "A6", "A7"):
        assert cid in cp.stdout, f"--list 未列出 {cid}"


def test_missing_modules_are_fail_not_pass(tmp_path):
    stub = _write_stub(tmp_path / "stub", {"src/kfpa/__init__.py": ""})
    rep = _report(tmp_path, stub, "--only", "A1")
    a1 = _by_id(rep, "A1")
    assert a1["status"] == "FAIL", "模块缺失时 A1 竟然不是 FAIL —— 台架会假绿！"
    text = _findings(a1)
    assert "src/kfpa/cli.py" in text, "缺失诊断没有指名 src/kfpa/cli.py"
    assert "磐石" in text or "界面" in text, "缺失诊断没有指名责任模块"


def test_planted_model_string_is_caught(tmp_path):
    stub = _write_stub(tmp_path / "stub", {
        "src/kfpa/__init__.py": "",
        "src/kfpa/oops.py": 'DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"\n',
    })
    rep = _report(tmp_path, stub, "--only", "A5.1")
    a5 = _by_id(rep, "A5.1")
    assert a5["status"] == "FAIL", "投毒的模型字符串没被 A5.1 抓到"
    assert "oops.py" in _findings(a5)


def test_adapter_whitelist_is_not_a_false_positive(tmp_path):
    stub = _write_stub(tmp_path / "stub", {
        "src/kfpa/__init__.py": "",
        "src/kfpa/llm.py": 'MODEL = "deepseek/deepseek-v4.1-flash"  # 适配器内部\n',
    })
    rep = _report(tmp_path, stub, "--only", "A5.1")
    assert _by_id(rep, "A5.1")["status"] == "PASS", "llm.py 白名单被误杀"


def test_contract_version_drift_is_flagged(tmp_path):
    stub = _write_stub(tmp_path / "stub", {
        "src/kfpa/__init__.py": "",
        "src/kfpa/schemas.py": 'CONTRACT_VERSION = "9.9"\n',
    })
    rep = _report(tmp_path, stub, "--only", "A1")
    a1 = _by_id(rep, "A1")
    assert "9.9" in _findings(a1), "契约版本漂移没被点名"


def test_real_repo_passes_model_string_iron_rule(tmp_path):
    """真仓库（main + 各人分支合并后）必须满足 A5.1；失败说明有人把模型串写进了业务代码。"""
    rep = _report(tmp_path, ROOT, "--only", "A5.1")
    assert _by_id(rep, "A5.1")["status"] == "PASS", _findings(_by_id(rep, "A5.1"))

"""执行器（P0，契约 §3.6 + D1-7）。

- `Executor`：协议（`name` + `apply(package, workdir) -> ExecResult`）
- `MockExecutor`：**验收路径**。确定性、无网络、无 key，按任务包生成产物并回填
  `results`（验收断言 cid → 是否通过）；支持失败注入 `none | apply_fail | test_fail`（D1-7）
- `AiderExecutor`：可选适配器，subprocess 调 aider；缺包/缺 key 时返回 ok=False + 中文
  error（三段式），不崩

失败注入（供 A4 验收；两种入口等价）：
    构造参数 `MockExecutor(injection="test_fail")`，或环境变量 `KFPA_MOCK_INJECT`。
    `none`       全部通过
    `apply_fail` ok=False + error_category=apply_failed（results 不回填 → G2 短路）
    `test_fail`  ok=False + error_category=test_fail + failed_criteria=[首条断言]
    `KFPA_MOCK_FAIL_TIMES=N` 只让前 N 次调用注入失败，之后转成功（可选）
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from .schemas import ExecResult, TaskPackage

#: 允许的注入档位（D1-7）
INJECTIONS: tuple[str, ...] = ("none", "apply_fail", "test_fail")


@runtime_checkable
class Executor(Protocol):
    """执行器协议：把任务包应用到一个工作目录，返回执行结果。"""

    name: str

    def apply(self, package: TaskPackage, workdir: str | Path) -> ExecResult:  # pragma: no cover - 协议
        ...


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _tail(text: str | None, limit: int = 600) -> str:
    body = " ".join((text or "").split())
    return body[-limit:] if len(body) > limit else body


class MockExecutor:
    """离线验收执行器：按任务包确定性产出文件，永不联网。

    产物（相对 workdir，路径稳定、内容不含时间戳 → 可复算；D1-10「只准写这个目录」）：
        <feature>.mock.txt   实现产物占位
        result.md            执行报告
    """

    name = "mock"

    def __init__(self, injection: str = "none") -> None:
        self.injection = injection if injection in INJECTIONS else "none"
        self._calls = 0

    def _current_injection(self) -> str:
        value = self.injection if self.injection != "none" else _env("KFPA_MOCK_INJECT")
        return value if value in INJECTIONS else "none"

    def _injecting_now(self) -> bool:
        raw = _env("KFPA_MOCK_FAIL_TIMES")
        if not raw:
            return True
        try:
            times = int(raw)
        except ValueError:
            return True
        return self._calls <= times

    def apply(self, package: TaskPackage, workdir: str | Path) -> ExecResult:
        self._calls += 1
        root = Path(workdir)
        root.mkdir(parents=True, exist_ok=True)
        cids = [c.cid for c in package.acceptance]
        target = root / f"{package.feature}.mock.txt"
        criteria_lines = "\n".join(f"- {c.cid}: {c.text}" for c in package.acceptance) or "- （无验收条目）"
        injection = self._current_injection()
        if injection != "none" and not self._injecting_now():
            injection = "none"

        if injection == "apply_fail":
            error = "[executor] apply_fail 注入：本次应用未落地 → 检查注入档位与工作目录"
            (root / "result.md").write_text(
                f"# 第 {package.round_no} 轮执行报告（mock）\n\n- 状态：FAILED\n- 档位：apply_fail\n",
                encoding="utf-8",
            )
            return ExecResult(
                ok=False,
                files_changed=[],
                diff="",
                error=error,
                error_category="apply_failed",
                failed_criteria=[],
                results={},
                executor=self.name,
            )

        if injection == "test_fail":
            failed = cids[:1] or ["<无验收条目>"]
            results = {cid: (cid not in failed) for cid in cids}
            error = (
                f"[executor] test_fail 注入：断言 {failed[0]} 未通过 → 按门禁 G3 走回灌重试\n"
            )
            (root / "result.md").write_text(
                f"# 第 {package.round_no} 轮执行报告（mock）\n\n"
                f"- 状态：FAILED\n- 档位：test_fail\n- 未通过断言：{'、'.join(failed)}\n",
                encoding="utf-8",
            )
            return ExecResult(
                ok=False,
                files_changed=[],
                diff="",
                error=error,
                error_category="test_fail",
                failed_criteria=list(failed),
                results=results,
                executor=self.name,
            )

        body = (
            f"# mock 产物 · round {package.round_no}\n\n"
            f"## 目标\n{package.goal}\n\n"
            f"## 验收标准（逐条落实现）\n{criteria_lines}\n\n"
            f"## 边界条件\n"
            + "\n".join(f"- {b}" for b in package.boundaries)
            + "\n\n## 接口契约\n"
            + "\n".join(f"- {i}" for i in package.interface_contract)
            + "\n\n## 禁止事项\n"
            + "\n".join(f"- {p}" for p in package.prohibitions)
            + "\n\n## 回灌区\n"
            + ("\n".join(f"- {f}" for f in package.feedback) if package.feedback else "- 无（首轮）")
            + "\n"
        )
        target.write_text(body, encoding="utf-8")
        rel = target.relative_to(root).as_posix()
        diff = _env("KFPA_MOCK_DIFF") or (
            f"--- /dev/null\n+++ b/{rel}\n"
            f"@@ mock 新增 {len(body.splitlines())} 行 @@\n"
            f"+ 依任务包 {package.goal[:40]} 生成\n"
        )
        (root / "result.md").write_text(
            f"# 第 {package.round_no} 轮执行报告（mock）\n\n"
            f"- 状态：OK\n- 产物：{rel}\n- 验收项：{len(cids)} 条全部通过\n",
            encoding="utf-8",
        )
        return ExecResult(
            ok=True,
            files_changed=[rel],
            diff=diff,
            error="",
            error_category="",
            failed_criteria=[],
            results={cid: True for cid in cids},
            executor=self.name,
        )


class AiderExecutor:
    """Aider 适配器（可选，不进 P0 验收路径）。

    - 缺 aider 可执行文件 → ok=False + error_category=apply_failed + 三段式中文 error；
    - 模型名只从环境变量 `KFPA_AIDER_MODEL` 读，代码里不出现任何模型字符串（A5 铁律）；
    - subprocess 异常全部兜住，不让流水线崩。
    """

    name = "aider"

    def __init__(self, timeout: int = 600) -> None:
        self.timeout = timeout

    @staticmethod
    def _message(package: TaskPackage) -> str:
        lines = [f"目标：{package.goal}", "", "验收标准："]
        lines += [f"- {c.cid}: {c.text}" for c in package.acceptance]
        lines += ["", "边界条件："] + [f"- {b}" for b in package.boundaries]
        lines += ["", "接口契约："] + [f"- {i}" for i in package.interface_contract]
        lines += ["", "禁止事项："] + [f"- {p}" for p in package.prohibitions]
        if package.feedback:
            lines += ["", "回灌区（上一轮失败原因与规避要求）："] + [f"- {f}" for f in package.feedback]
        return "\n".join(lines)

    def apply(self, package: TaskPackage, workdir: str | Path) -> ExecResult:
        exe = shutil.which("aider")
        if not exe:
            return ExecResult(
                ok=False,
                error=(
                    "[executor] 未找到 aider 可执行文件 → P0 验收路径请用 MockExecutor（--dry-run），"
                    "安装 aider 并配置 key 后再启用本适配器"
                ),
                error_category="apply_failed",
                executor=self.name,
            )

        cmd = [exe, "--yes", "--no-auto-commits", "--message", self._message(package)]
        model = _env("KFPA_AIDER_MODEL")
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            return ExecResult(
                ok=False,
                error="[executor] aider 启动失败 → 可执行文件不可用，请检查安装",
                error_category="apply_failed",
                executor=self.name,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                ok=False,
                error=f"[executor] aider 执行超过 {self.timeout}s 已放弃本轮 → 缩小任务包或调大超时",
                error_category="apply_failed",
                executor=self.name,
            )
        except OSError as exc:
            return ExecResult(
                ok=False,
                error=f"[executor] aider 启动异常：{exc} → 检查执行环境",
                error_category="apply_failed",
                executor=self.name,
            )

        stdout = _tail(proc.stdout, 2000)
        stderr = _tail(proc.stderr, 1000)
        if proc.returncode != 0:
            return ExecResult(
                ok=False,
                diff=stdout,
                error=(
                    f"[executor] aider 退出码 {proc.returncode}：{stderr or stdout or '无输出'}"
                    " → 检查 aider 配置与 key"
                ),
                error_category="apply_failed",
                executor=self.name,
            )
        return ExecResult(
            ok=True, files_changed=[], diff=stdout, error="", error_category="", executor=self.name
        )


__all__ = ["INJECTIONS", "Executor", "MockExecutor", "AiderExecutor"]

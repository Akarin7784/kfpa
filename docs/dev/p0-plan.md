# KFPA P0 分工与接口契约（v1 · 冻结）

> 本轮目标：按 docs/design.md §8 路线图 **P0 懒人版**，把第一轮生命周期打通并可离线验收。
> 契约版本：`CONTRACT_VERSION = 1.0`（`src/kfpa/schemas.py`，Chief 冻结）

## 0. 本轮范围

跑通一条竖切（全程可在**无网络、无 API key** 的 dry-run 下验收）：

```
设计文本 → 質問票（歧义报告）→ 人工确认 → 编码任务包（六段式）
        → 执行器（mock；Aider 为可选适配器）→ 门禁三态 → SQLite 落库 → 轮次快照
```

**本轮不做**（P1/P2 内容）：Dify RAG、Langfuse、PR-Agent、PostgreSQL、真实模型 API 调用、
真 Aider 执行的成功性。`litellm` / `aider` / `streamlit` 只做**惰性导入的可选适配器**，
不进入验收路径，缺包时必须优雅报错而不是 ImportError 崩溃。

## 1. 工作纪律

1. **一人一工作树**。共享同一文件系统，禁止多人共用一个 clone：

   | 成员 | clone 目录 | 分支 | 写权限范围（唯一写者） |
   |---|---|---|---|
   | 磐石 | `C:\Users\Administrator\.dsh\bot\workspace\kfpa-panshi` | `p0/core` | `pyproject.toml`、`src/kfpa/{config,store,llm,cli,__main__}.py`、`src/kfpa/__init__.py`、`schema/` |
   | 长流 | `...\kfpa-changliu` | `p0/pipeline` | `src/kfpa/{pipeline,gates,executor}.py`、`src/kfpa/prompts/**`、`examples/**` |
   | 界面 | `...\kfpa-jiemian` | `p0/ui` | `src/kfpa/ui/**` |
   | 镜鉴 | `...\kfpa-jingjian` | `p0/verify` | `tests/**`、`scripts/verify*.py`、`docs/acceptance/**` |

2. **不得越界写别人的文件**。需要别人改接口 → 群里提，Chief 裁。
3. **`src/kfpa/schemas.py` 只读**（Chief 冻结）。要加字段/改签名，先在群里说明理由。
4. **交付走 PR**：分支 → PR（描述里必须写「验收命令」）→ 镜鉴独立验证并评论结论 → Chief 合并。
   未通过独立验证的 PR 不合。
5. **依赖由 Chief 统一装**（系统 Python 3.14）。任何人不要在各自目录里 `pip install` 或建 venv。
6. 所有产出用中文注释/中文文档，与仓库既有风格一致。

## 2. 模块布局

```
pyproject.toml            # 磐石
src/kfpa/__init__.py      # 磐石（已存在）
src/kfpa/schemas.py       # Chief 冻结（已存在）
src/kfpa/config.py        # 磐石：models.yaml 加载 + 角色校验
src/kfpa/store.py         # 磐石：SQLite 持久化（rounds/tickets/packages/gates/snapshots/failures）
src/kfpa/llm.py           # 磐石：角色路由 + MockLLM + LiteLLM 适配器
src/kfpa/cli.py           # 磐石：argparse 主入口，聚合各子命令
src/kfpa/__main__.py      # 磐石：python -m kfpa
src/kfpa/pipeline.py      # 长流：轮次生命周期编排
src/kfpa/gates.py         # 长流：门禁三态判定
src/kfpa/executor.py      # 长流：MockExecutor + AiderExecutor
src/kfpa/prompts/*.j2     # 长流：六段式任务包 / 質問票 / 评审模板
src/kfpa/ui/cli.py        # 界面：tickets 子命令（register(subparsers)）
src/kfpa/ui/streamlit_app.py  # 界面：質問票台账（可选，不验收）
examples/design-r1.md     # 长流：示例原始设计（dry-run 输入）
tests/**                  # 镜鉴
```

## 3. 冻结接口

### 3.1 数据模型 —— `src/kfpa/schemas.py`（已冻结）

- 枚举：`GateState{PASS,NEEDS_FIX,BLOCKED}`、`TicketImpact{HIGH,MEDIUM,LOW}`、
  `TicketStatus{OPEN,CLOSED,DROPPED}`、`RoundState{DESIGN_IN,TICKETS_OPEN,RELEASED,EXECUTING,GATED,SNAPSHOTTED,BLOCKED}`
- `QATicket`：票面五要素 `ticket_id/location/option_a/option_b/architect_preference`
  + `impact/status/answer/round_no`；`missing_fields()` / `is_open_high()`
- `TaskPackage`：六段 `goal/acceptance/boundaries/interface_contract/prohibitions/feedback`
  + `closed_tickets`；`missing_sections()` / `is_complete()`；`package_hash(pkg)`
- `ExecResult(ok, files_changed, diff, error, executor)`
- `GateReport(round_no, state, reasons, failing_criteria, attempt, escalated, same_reason_count)`
- `FailureRecord(round_no, attempt, reason, avoid_pattern)`
- `Snapshot(round_no, package_hash, gate_state, git_sha, tag)`
- `Round` / `RoundResult`
- 工具：`to_jsonable()` / `dumps()`

### 3.2 存储 —— `src/kfpa/store.py`（磐石）

```python
class Store:
    def __init__(self, db_path: str | Path) -> None: ...
    def init_schema(self) -> None: ...
    # 轮次
    def upsert_round(self, round_: Round) -> Round: ...
    def get_round(self, round_no: int) -> Round | None: ...
    def set_round_state(self, round_no: int, state: RoundState) -> None: ...
    # 質問票
    def add_ticket(self, ticket: QATicket) -> QATicket: ...
    def get_ticket(self, ticket_id: str) -> QATicket | None: ...
    def list_tickets(self, round_no: int | None = None,
                     status: TicketStatus | None = None) -> list[QATicket]: ...
    def close_ticket(self, ticket_id: str, answer: str) -> QATicket: ...
    def list_open_high_tickets(self, round_no: int) -> list[QATicket]: ...
    # 任务包 / 门禁 / 失败 / 快照
    def save_package(self, package: TaskPackage) -> str: ...      # 返回 package_hash
    def get_package(self, round_no: int) -> TaskPackage | None: ...
    def save_gate_report(self, report: GateReport) -> None: ...
    def list_gate_reports(self, round_no: int) -> list[GateReport]: ...
    def record_failure(self, failure: FailureRecord) -> None: ...
    def list_avoid_patterns(self, round_no: int | None = None) -> list[str]: ...
    def count_same_failure(self, round_no: int, reason_key: str) -> int: ...
    def save_snapshot(self, snapshot: Snapshot) -> None: ...
    def list_snapshots(self) -> list[Snapshot]: ...
```

要求：SQLite（标准库 `sqlite3`）；`db_path=":memory:"` 必须可用（测试用）；JSON 字段用 `schemas.dumps`。

### 3.3 模型路由 —— `src/kfpa/llm.py`（磐石）

```python
class LLM(Protocol):
    def complete(self, role: str, system: str, user: str, *, json_mode: bool = False) -> str: ...

def load_roles(config_path: str | Path = "config/models.yaml") -> dict[str, dict]: ...
def build_llm(roles: dict[str, dict], provider: str = "mock", **kwargs) -> LLM: ...
class MockLLM:      # 确定性、无网络、无 key，按 role 返回固定剧本
class LiteLLMAdapter:  # 惰性 import litellm；缺包时 raise RuntimeError（中文提示）
```

- `role` 只接受 `ARCHITECT / REVIEWER / FLASH / EMBEDDING`，其它一律报错。
- **模型字符串只允许出现在 `llm.py` 的适配器内部与 `config/models.yaml`**；
  `pipeline.py` / `gates.py` / `ui/**` 只准用角色名。

### 3.4 流水线 —— `src/kfpa/pipeline.py`（长流）

```python
def run_round(round_no: int, design_text: str, design_ref: str, *,
              store: Store, llm: LLM, executor: Executor,
              workdir: str | Path, max_attempts: int = 3,
              dry_run: bool = True) -> RoundResult: ...
def generate_tickets(round_no: int, design_text: str, llm: LLM) -> list[QATicket]: ...
def build_task_package(round_no: int, design_text: str, tickets: list[QATicket],
                       llm: LLM, avoid_patterns: list[str]) -> TaskPackage: ...
```

硬规则：`store.list_open_high_tickets(round_no)` 非空时，`build_task_package` 必须拒绝
（返回/抛出可判定的错误，CLI 退出码非零）。

### 3.5 门禁 —— `src/kfpa/gates.py`（长流）

```python
def evaluate(round_no: int, package: TaskPackage, exec_result: ExecResult,
             store: Store, attempt: int = 1) -> GateReport: ...
```

判据必须量化、可测，禁止模型拍脑袋：

1. `exec_result.ok is False` → `NEEDS_FIX`，`failing_criteria` 填失败项，`reason` 写入 `FailureRecord`
2. 任务包 `missing_sections()` 非空 → `NEEDS_FIX`（缺段本身是门禁失败）
3. 接受标准未满足（由执行器声明的 `failed_criteria` 或断言语义）→ `NEEDS_FIX`
4. 当前 reason 与历史同类失败 `same_reason_count >= 2` → `BLOCKED` + `escalated=True`
5. 全过 → `PASS`

### 3.6 执行器 —— `src/kfpa/executor.py`（长流）

```python
class Executor(Protocol):
    name: str
    def apply(self, package: TaskPackage, workdir: str | Path) -> ExecResult: ...

class MockExecutor:   # 确定性：按任务包生成产物文件（用于离线验收）
class AiderExecutor:  # subprocess 调 aider；缺包/缺 key 时返回 ok=False+中文 error，不崩
```

### 3.7 交互层 —— `src/kfpa/ui/**`（界面）

```python
# src/kfpa/ui/cli.py
def register(subparsers) -> None: ...   # 注册 `tickets` 子命令组
```

- `kfpa tickets list [--round N] [--impact HIGH] [--status OPEN]`：表格输出票号/轮次/位置/影响面/状态/倾向
- `kfpa tickets close <ticket_id> --answer "<人工确认>"`：关闭并入库，回显票面与结论
- `kfpa tickets batch-close --file <answers.yaml|json>`：攒批确认（design.md §5.1 保护人的确认带宽）
- `src/kfpa/ui/streamlit_app.py`：台账页面，惰性 import streamlit（缺包给中文提示），**不列入验收**

### 3.8 CLI 主入口 —— `src/kfpa/cli.py`（磐石）

```python
def main(argv: list[str] | None = None) -> int: ...   # 返回退出码
```

子命令（磐石实现 `init / round / gate / history / rollback`，界面提供 `tickets`）：

- `kfpa init [--db PATH]`：建库建表
- `kfpa round --design <file> --round N [--dry-run] [--db PATH] [--workdir PATH] [--provider mock|litellm]`
- `kfpa history [--db PATH]`：列轮次快照（轮次/状态/package_hash/门禁态）
- `kfpa rollback <round> [--db PATH]`：按快照回滚（P0 到「记录 + `git checkout` 提示」灰度即可）
- `kfpa tickets ...`：由 `kfpa.ui.cli.register()` 注入

默认 `--db kfpa.db`，默认 `--dry-run`（P0 验收路径）。

## 4. 验收标准（镜鉴独立验证，逐条给可复现证据）

| 编号 | 断言 | 验收命令（示例） |
|---|---|---|
| A1 | CLI 可用：`python -m kfpa --help` 退出码 0，含 `init/round/tickets/history` | `python -m kfpa --help` |
| A2 | **离线端到端**：dry-run 跑完整轮，質問票 ≥1 张且票面五要素齐全、任务包六段齐全、产出执行结果、门禁有态、SQLite 各表有记录 | `python -m kfpa init --db t.db` && `python -m kfpa round --design examples/design-r1.md --round 1 --dry-run --db t.db` |
| A3 | 硬规则：存在未关闭高影响票时**不得**生成任务包 → 退出码非零 + 中文错误；关闭后可继续 | 构造高影响票后跑 round |
| A4 | 三态闭环：执行失败 → `NEEDS_FIX` 且回灌区非空；同类失败 ≥2 次 → `BLOCKED` + `escalated=True` | 注入失败用例 |
| A5 | 模型绑定铁律：`src/` 业务代码内不出现模型字符串（`deepseek`/`gpt`/`claude`/`qwen`/`bge`），仅 `llm.py` 适配器与 config 白名单 | `grep -riE "deepseek\|gpt-\|claude\|qwen\|bge" src --include=*.py` |
| A6 | 快照可查：每轮落 `snapshots` 表，`kfpa history` 可列出 | `python -m kfpa history --db t.db` |
| A7 | 質問票台账可读写：`tickets list` 输出含票号/位置/影响面/状态；`tickets close` 关闭入库 | `python -m kfpa tickets list --db t.db` |

验证要求：
- 除 A2 的 `--dry-run` 外，**不得**依赖网络、API key、litellm/aider/streamlit 安装。
- 镜鉴必须给出「命令 + 真实输出片段 + 结论」，不许只写「已通过」。
- 断言之间独立；失败必须能指出是哪个模块、哪条接口。

## 5. PR 与合并

1. 各自分支推 PR，标题 `p0/<模块>: <一句话>`，描述含：改了什么 / 怎么验 / 已知未做。
2. 镜鉴在 PR 评论给出 A1–A7 中与本次改动相关的结论（通过/不通过 + 证据）。
3. Chief 复核 diff 后合并（无阻塞即合）；合并后由镜鉴对 main 再跑一次端到端。
4. 出现接口冲突：先群里说，Chief 改契约并升 `CONTRACT_VERSION`，不要各自就地改。

## 6. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 1.0 | 2026-10-16 | 首版冻结：模块布局、接口、验收 A1–A7、分工 | Chief |

# KFPA P0 设计补充件（D1 · 冻结 v1.0）

> 定位：`docs/design.md` 与 `docs/spec.md` **没有提到的工程细节**，由 Chief 设计并在此冻结。
> 效力：P0 范围内与主文档冲突处，以本件为准；主文档的机制、角色、哲学不做改动。
> 关联：`docs/dev/p0-plan.md`（施工图 · 分支/写者/PR 纪律）、`src/kfpa/schemas.py`（CONTRACT_VERSION 1.1）

---

## D1-1 質問票生成判据（谁开、何时开、开多少）

开票者固定为 `ARCHITECT`，时机是「转译设计时」。命中以下任一即**必须**开票（不允许"看着差不多就跳过"）：

| 编号 | 判据 | 例 |
|---|---|---|
| Q1 | 同名不同义：同一名词在两处给出不同字段/类型/口径 | §3 的「批次」是入库批次，§5 指配送批次 |
| Q2 | 量词/范围未闭合 | 「若干」「等」「适当」「必要时」「定期」「T+1」未给时区/时点 |
| Q3 | 异常语义未定义 | 写了「失败则回滚」但没说回滚粒度或补偿方式 |
| Q4 | 外部接口无契约 | 外部系统、文件名、字段格式、编码未给 |
| Q5 | 边界缺失 | 数值上限、精度、并发数、幂等键未给 |
| Q6 | 章节冲突 | 同一动作在 §2 与 §7 结果不同 |
| Q7 | 高风险表述含糊 | 权限、金额、库存数量的判定口径含糊 |

**票面机读约定**（离线 fixture 与 MockLLM 共用，真实模型走同一 JSON 结构）：

```
QA: <位置> | A: <解释A> | B: <解释B> | P: <主BOT倾向> | I: HIGH|MEDIUM|LOW
```

**票号规则**：`R{round}-{seq:03d}`（`R1-001`），seq 从 001 递增、一轮内不复用。
票号由 `pipeline.py` 分配，模型与 mock 都不许自己编票号。

**开票上限**：单轮 > 20 张时，只保留 HIGH/MEDIUM，LOW 合并成一条汇总票——保护人的确认带宽（design.md §1.2）。

**硬规则**：存在 `status=open` 且 `impact=high` 的票时，`build_task_package` 必须拒绝执行（退出码 3）。

---

## D1-2 影响面分级（impact 的判定口径）

| 级别 | 判据（任一命中即该级） | 后果 |
|---|---|---|
| HIGH | 改数据模型 / 改接口契约 / 波及 ≥2 个模块 / 涉及金额、库存数量、权限、审计 | 人必须确认才可编码 |
| MEDIUM | 单模块内的行为分叉，可通过一轮返工吸收 | 攒批确认，可放行 |
| LOW | 文案、命名、日志、注释 | 攒批确认，可放行 |

**兜底规则**（模型判定之外的第二道闸，防"该 HIGH 的票被降级"）：
出现「新增/删除字段」「新增/删除接口」「改主键/唯一键」→ 至少 HIGH；
出现「字段/表/接口/权限/金额/数量」→ 至少 MEDIUM。

---

## D1-3 门禁量化判据（三态判定，纯确定性函数）

`gates.evaluate()` 按顺序短路判定，**不允许 LLM 参与判定**（design.md §3.1-5「禁止模型拍脑袋」）：

| 序 | 条件 | 结果 |
|---|---|---|
| G1 | `package.missing_sections()` 非空 | `NEEDS_FIX`，`failing_criteria = 缺段名` |
| G2 | `exec_result.ok is False` | `NEEDS_FIX`，按 `error_category` 定类 |
| G3 | `exec_result.failed_criteria` 非空 | `NEEDS_FIX`，逐条填 `failing_criteria` |
| G4 | `acceptance` 中存在 cid 不在 `exec_result.results` 里（断言未映射） | `NEEDS_FIX`，`error_category=unmapped_criterion` |
| G5 | `store.count_same_failure(round, reason_key) + 1 >= 2` | `BLOCKED`，`escalated=True` |
| G6 | 以上皆否 | `PASS` |

- **P0 用"断言映射完整性"代替代码覆盖率**：覆盖率数字门槛（行 ≥80%、关键路径 100%）登记到 P1，见 D1-13。
- 门禁必须把 `attempt`、`reason_key`、`same_reason_count` 写进 `GateReport` 并落库（可复盘）。
- `REVIEWER` 的评审意见在 P0 只是**输入**（可选），不单独决定三态；折算规则登记 P1。

---

## D1-4 失败归一化与升级口径

**归一化键**：`reason_key = sha1(f"{category}|{target}")[:12]`

- `category` 白名单：`apply_failed | compile | test_fail | missing_section | unmapped_criterion | other`
  （取 `ExecResult.error_category`；非法值一律归 `other`）
- `target` 取 `failed_criteria[0]`；为空则取 `files_changed[0]` 的目录；再为空取 `"round"`
- 规范化：小写、压空白、路径分隔统一 `/`、剔除行号与时间戳（`:\d+`、`#\d+`、`\d{10,}`）

**升级口径**（对应 design.md §5.3「同类失败 ≥2 次自动升级」）：
`same_reason_count` = 本库中该 `reason_key` 在**本轮历史**里的出现次数（不含本次）；
`escalated = (same_reason_count + 1) >= 2`，即**同一原因第二次失败即升级为阻塞**，不再让 Flash 空转。

**回避模式清单**（负知识）生成固定句式：

```
AVOID[{category}] {target}：上次失败——{error 摘要 ≤80 字}
```

`gates` 负责生成并写 `failures.avoid_pattern`；`pipeline` 在下一轮把 `avoid_patterns` 注入任务包 `feedback` 段（D1-11）。

---

## D1-5 重试上限与退出码

- `max_attempts = 3`（`--max-attempts` 可覆盖）：一轮最多 3 次执行尝试。
- 同类第 2 次失败 → `BLOCKED`（D1-4）；不同原因连续失败到上限 → `BLOCKED`，`reason=attempts_exhausted`。
- 每次失败都必须产生 `FailureRecord`（不许"静默重试"）。

CLI 退出码（必须写进 `kfpa --help`）：

| 码 | 含义 |
|---|---|
| 0 | PASS（并通过快照） |
| 10 | NEEDS_FIX（未耗尽重试上限） |
| 11 | BLOCKED（升级主BOT） |
| 3 | 存在未关闭的高影响質問票，拒绝生成任务包 |
| 2 | 用法/参数错误 |
| 4 | 依赖缺失（litellm/aider/streamlit 等可选包未安装） |
| 5 | I/O 或数据库错误 |

---

## D1-6 快照与回滚（P0 语义）

- **只有 `PASS` 轮打 tag**：`kfpa-r{n}`（annotated tag，message 含 `package_hash` 与 `gate_state`）。
- `snapshots` 表一轮一行：`package_hash` + `git_sha(HEAD)` + `gate_state` + `tag`。
- 失败的尝试**不**打 tag，但 `failures` / `gate_reports` 全留痕。
- `kfpa rollback <round>`：默认**只打印**将要执行的命令；加 `--apply` 才真执行 `git checkout kfpa-r{n}`；
  找不到快照/tag → 退出码 5，不做任何写操作。
- `kfpa history`：按轮列出 `round_no | state | package_hash | gate_state | tag`。

---

## D1-7 Mock 剧本 —— 离线验收的基石

> **本节的 marker 字面量已由 D2 R-17 改口径**：规范 marker 是 `KFPA_TASK=qa_tickets` /
> `KFPA_TASK=task_package`；本节下表里的 `[KFPA:GEN_TICKETS]` / `[KFPA:BUILD_PACKAGE]` /
> `[KFPA:REVIEW]` 降为**兼容别名，永久保留，不许删**。其余内容（抽机读行、同输入同输出、
> 未知 marker 抛 `MockScriptError`）继续有效。

MockLLM **不推理**：它按 prompt 里的 marker 从输入文本抽机读行，同一输入永远同一输出。
设计文本（`examples/*.md`）里的机读行：

```
FEATURE: <功能名>
GOAL: <一句话目标>
AC: <断言>                     # 可多条
BOUND: <边界条件>               # 可多条
IF: <接口契约行>                # 可多条
NO: <禁止事项>                  # 可多条
QA: <位置> | A: ... | B: ... | P: ... | I: HIGH|MEDIUM|LOW
```

marker 与行为：

| marker | 角色 | 返回 |
|---|---|---|
| `[KFPA:GEN_TICKETS]` | ARCHITECT | 抽 `QA:` 行 → JSON 数组（无 `ticket_id`，由 pipeline 编号）；无匹配行 → `[]` |
| `[KFPA:BUILD_PACKAGE]` | ARCHITECT | 抽 `FEATURE/GOAL/AC/BOUND/IF/NO` 行 → 六段 JSON；`feedback` 由传入的 `avoid_patterns` 填 |
| `[KFPA:REVIEW]` | REVIEWER | `{"verdict":"pass"\|"fail","error_category":...,"failed_criteria":[...],"reason":"..."}` |

- 未知 marker、或 marker 与 role 不匹配 → 抛 `MockScriptError`（中文提示）。**禁止静默返回空串**。
- 失败注入：`--inject none|apply_fail|test_fail`，由 CLI 传给 `MockExecutor` / 评审剧本，供验收 A4 使用。
- MockLLM 只用于 dry-run 与测试；`--provider litellm` 走真实适配器（不列入 P0 验收）。

---

## D1-8 攒批确认文件格式（batch-close）

`kfpa tickets batch-close --file answers.yaml`，文件是 `票号 -> 人工结论` 的映射（YAML 或 JSON 皆可）：

```yaml
R1-001: "采用 B：按 24 小时整点结算"
R1-003: "采用 A，并补充：超时按自然日回退"
```

规则：未知票号 → 退出码 2 且**整体不写**（事务）；答案为空 → 拒绝该条；
已关闭票重复提交 → 幂等忽略并提示。确认结果写入 `tickets.answer` + `status=closed`，
并在下一轮任务包的 `feedback`/`closed_tickets` 里带上（消歧依据）。

台账输出列（`tickets list`）：
`ticket_id | round | impact | status | location | architect_preference | answer`

---

## D1-9 SQLite 表结构（六表 + meta）

```sql
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS rounds (
  round_no INTEGER PRIMARY KEY, design_ref TEXT NOT NULL,
  state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS tickets (
  ticket_id TEXT PRIMARY KEY, round_no INTEGER NOT NULL, location TEXT NOT NULL,
  option_a TEXT NOT NULL, option_b TEXT NOT NULL, architect_preference TEXT NOT NULL,
  impact TEXT NOT NULL, status TEXT NOT NULL, answer TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tickets_round ON tickets(round_no, status, impact);

CREATE TABLE IF NOT EXISTS task_packages (
  round_no INTEGER PRIMARY KEY, package_hash TEXT NOT NULL,
  payload TEXT NOT NULL, created_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS gate_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, round_no INTEGER NOT NULL, attempt INTEGER NOT NULL,
  state TEXT NOT NULL, reason_key TEXT NOT NULL DEFAULT '',
  reasons TEXT NOT NULL, failing_criteria TEXT NOT NULL,
  escalated INTEGER NOT NULL DEFAULT 0, same_reason_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_gate_round ON gate_reports(round_no, attempt);

CREATE TABLE IF NOT EXISTS failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, round_no INTEGER NOT NULL, attempt INTEGER NOT NULL,
  reason TEXT NOT NULL, avoid_pattern TEXT NOT NULL, reason_key TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_failures_key ON failures(round_no, reason_key);

CREATE TABLE IF NOT EXISTS snapshots (
  round_no INTEGER PRIMARY KEY, package_hash TEXT NOT NULL, gate_state TEXT NOT NULL,
  git_sha TEXT NOT NULL DEFAULT '', tag TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
```

`meta.schema_version = "1"`；JSON 列一律用 `schemas.dumps()`，时间戳一律 Unix epoch 秒（UTC）。
`:memory:` 必须可用（测试用）。

---

## D1-10 目录与运行约定

- DB 默认 `./kfpa.db`（`--db` 覆盖，支持 `:memory:`）。
- 执行器工作目录默认 `./runs/round-{n}`（`--workdir` 覆盖）；**执行器只准写这个目录**。
- 日志：每阶段一行 `[i/6] 阶段名 …`；`--json` 时最后输出 `schemas.dumps(RoundResult)` 单行 JSON。
- 编码 UTF-8 + LF（`.gitattributes` 由磐石加，避免 Windows CRLF 干扰 diff 与快照指纹）。
- 时间展示用本地时区，存储一律 epoch（UTC）。
- 命令行中文提示，错误信息必须含「模块 + 原因 + 下一步」三段（例：`[gate] 断言 AC-2 未映射 → 检查执行器 results 回填`）。

---

## D1-11 负知识最小闭环（P0 必做）

`失败 → failures 表 → 下一轮 build_task_package(avoid_patterns=...) → 任务包 feedback 段非空`。
这是 design.md「回灌区」在 P0 唯一的**可实证**落点，验收 A4 必须证明：
第 2 次执行的任务包里 `feedback` 段带上了第 1 次的 `AVOID[...]` 行。

---

## D1-12 P0 状态机：照文档用 LangGraph（附「图可选、核心必测」约束）

**决策：P0 按 tech-stack.md 用 LangGraph 做轮次状态机**（`StateGraph`），
checkpoint 用 `InMemorySaver`（P0 单进程）；项目台账真相源仍是我们自己的 SQLite（D1-9），
P1 再换 `SqliteSaver`。理由：文档选型明确，且已验证在 Python 3.14 上可装可用（见下方环境清单）。

**工程约束（防止"图把实现锁死"）**：

1. 所有业务判定写成**纯函数**（`gates.evaluate` / `reason_key` / `package_hash` / 六段自检…），
   LangGraph 只负责编排：节点 = 调用纯函数 + Store + LLM/Executor；
2. 节点内**不许**写业务判据（判据只能来自纯函数，便于单测与独立验证）；
3. 验收 A1–A7 只依赖纯函数与 CLI，不依赖图的内部结构；图的冒烟另行验收（A8）；
4. 質問票的人确认卡点，P0 走 CLI 攒批（D1-8，离线可测），LangGraph 的 `interrupt` 语义留到 P1 接 Streamlit 时启用。

**已装环境（Chief 统一安装，勿自行 pip install/建 venv）**：
Python 3.14.x · `langgraph 1.2.15` · `langgraph-checkpoint-sqlite 3.1.2` · `litellm 1.105.1`
· `jinja2 3.1.6` · `PyYAML 6.0.3` · `pytest 9.1.1`。

**若 LangGraph 在 3.14 运行期出现阻塞问题**：保留 `pipeline.py` 的纯函数核心，把图降级为可选编排层，
不得影响 A1–A7 —— 这是本约束 1/2 的保险丝。

---

## D1-13 待设计登记（P1+，本轮明确不做）

1. 覆盖率数字门槛与测试从规约自动生成（行 ≥80%、关键路径 100%）
2. PR-Agent / REVIEWER 意见折算成 `failing_criteria` 的仲裁规则
3. 沉淀闸门参数：置信度阈值、抽检抽样率、隔离区转正条件
4. 快车道「影响分析」的自动判据算法（design.md §5.6 只给了定性判据）
5. Streamlit 質問票台账（P1）；P0 只做 CLI 台账
6. 异构评审位接入后的同源盲区度量方法
7. LiteLLM token 账本（Langfuse）与每轮成本归集

---

## 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 1.0 | 2026-10-16 | 首版冻结 D1-1 ~ D1-13；契约随之升到 CONTRACT_VERSION 1.1 | Chief |

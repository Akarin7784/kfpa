# KFPA P0 跨模块裁决记录（D2）

> 定位：各成员在实现中撞到的**跨模块语义分歧**，由 Chief 裁决并固化在此。
> 效力：与 `p0-plan.md` / `p0-design-addendum.md` 冲突处，以本件为准（本件更晚且更具体）。
> 裁决一旦落文，所有分支必须按此收敛；要翻案 → 群里说理由，Chief 出新版本。

---

## R-1 退出码：以 D1-5 为准（磐石的 0/1/2/3/4 作废）

**理由**：`2` 与 argparse 原生「用法错误」撞车；`BLOCKED` 与 `NEEDS_FIX` 必须可区分，CI 才能判读。

| 码 | 含义 | 备注 |
|---|---|---|
| 0 | PASS / 正常 | 含 `--help`、空库 `history` |
| 2 | 用法 / 参数错误 | argparse 原生，不要再占用 |
| 3 | 等待人工确认 | 有未关闭高影响票；或该轮暂无门禁记录 |
| 4 | 依赖 / 环境错误 | 含可选包缺失、配置错误、设计文件不存在 |
| 5 | I/O 或数据库错误 | 含快照/标签缺失导致的回滚失败 |
| 10 | NEEDS_FIX | 未耗尽重试上限 |
| 11 | BLOCKED | 已升级主BOT |

- 退出码映射**归 `src/kfpa/cli.py`（磐石）**，长流不要另做 `round_exit_code()`；
  长流只需保证 `RoundResult.state` 语义正确（阻塞 → `RoundState.BLOCKED`，待确认 → `TICKETS_OPEN`）。
- `--help` 的 epilog 必须列这张表。

## R-2 运行方式：统一 `PYTHONPATH=src`，不做全局 editable 安装

- PowerShell：`$env:PYTHONPATH="src"; python -m kfpa --help`
- bash：`PYTHONPATH=src python -m kfpa --help`
- `pytest` 由 `pyproject.toml` 的 `pythonpath = ["src"]` 兜住，直接 `python -m pytest`。
- **不做** `pip install -e .`：四个 clone 会互相顶掉 site-packages 的指向，谁后装谁是真理，必然出假红。
- 镜鉴的验收命令一律带该前缀；PR 描述里的验收命令也必须带。

## R-3 MockLLM 剧本：marker 优先、关键词兜底

- marker（长流往 prompt 的 system 里写）：`KFPA_TASK=qa_tickets` / `KFPA_TASK=task_package`。
- 无 marker 时，退回磐石已实现的关键词判定（質問票/歧义/ticket/qa、任务包/六段/package）。
- 返回结构以磐石现有实现为准：`{"tickets":[...]}` / `{feature,goal,acceptance,boundaries,interface_contract,prohibitions,feedback,closed_tickets}`。
- **`ticket_id` 由 `pipeline` 分配并覆盖 mock 返回值**（`R{round}-{seq:03d}`，D1-1）；mock 的 `T-001` 一律忽略。
  实现口径（防 `QATicket(**raw)` 的 duplicate keyword 炸）：`raw.pop("ticket_id", None)` 之后再构造，
  绝不要把 `ticket_id` 和 pipeline 生成的票号同时传进构造函数。
- 默认 2 张票、impact 不含 high（保 A2 一条命令跑完整轮）；A3 用 `build_llm(roles, provider="mock", high_impact=True)` 造高影响票。

## R-4 `count_same_failure` 口径与升级时点

- `store.count_same_failure(round_no, reason_key)` 返回**库中该 `reason_key` 的记录条数**
  （即：写入当前 `FailureRecord` 之后调用，得到含本次的计数）。
- gate 流程固定为：`record_failure(本次)` → `count = count_same_failure(...)` → `escalated = count >= 2`。
- `GateReport.same_reason_count = count - 1`（保持 D1-4「历史次数、不含本次」的字段语义），
  `GateReport.attempt` 记本次尝试号。

## R-5 REVIEWER 不进入 P0 门禁

- P0 门禁是纯确定性函数（D1-3），**不调用 REVIEWER**，不许因评审格式差异判 BLOCKED。
- REVIEWER 的 mock JSON 形状以磐石现有 `{"reviews":[{severity,location,issue,suggestion}]}` 为准；
  长流若要接评审位，必须宽容解析，解析失败只记日志、不改三态。

## R-6 票号与重跑语义

- 票号固定 `R{round}-{seq:03d}`（`R1-001`）。
- 同票号重跑：只刷新票面，**保留 `status` 与 `answer`**（A3「关闭后可继续」依赖此行为，磐石现有实现保留）。

## R-7 litellm 已装，缺包路径改测「异常包装」（新增验收 A9）

- 本机已装 `litellm 1.105.1`，所以「缺包报错」这条实际不可测——不要再为此写脆弱测试。
- 改为验收：**适配器把一切调用异常统一包成中文 `RuntimeError`**，消息里必须提示 `--provider mock`。
- **A9**：用假模型名 / 空 key 构造一次 `LiteLLMAdapter.complete()` 调用，断言
  ①抛 `RuntimeError`（不是 AuthenticationError 裸抛）②消息含中文提示与 `--provider mock`
  ③不产生网络长阻塞（可 mock 掉底层调用）。允许在无网络环境下 skip 真实调用，但**包装逻辑必须被断言**。

## R-8 `tickets list --impact` 下推到 Store（界面不要本地过滤）

- 界面问：§3.7 有 `--impact`，§3.2 的 `list_tickets(round_no, status)` 没有 impact 参数。
- **裁决：下推到 DB，界面不本地过滤。** `p0-plan.md` §3.2 的签名按本条读作：

  ```python
  def list_tickets(self, round_no: int | None = None,
                   status: TicketStatus | None = None,
                   impact: TicketImpact | None = None) -> list[QATicket]: ...
  ```

- 这是 **`store.py` 的方法签名**变更，不是数据契约变更 —— `schemas.py` 不动，
  **不升 `CONTRACT_VERSION`**（契约版本只管 `schemas.py` 里的数据结构）。
- 实施顺序：磐石在 PR #3 上补 `impact` 参数（SQL `WHERE` 过滤，索引 `ix_tickets_round` 已含 impact）→
  界面 rebase 后改成传参、删掉本地过滤 → 再合 #4。

## R-9 `tickets batch-close` 语义与退出码（禁止用 1）

- 未知票号 → **整体不写**，退出码 **2**（输入/用法错误）。
- 某条答案为空 → 视为输入错误，**整体不写**，退出码 **2**。
- 已关闭票重复提交 → **幂等跳过**，打印「跳过 + 原因」，退出码 **0**。
- 其余全部成功 → 退出码 **0**。
- **`1` 不再是合法退出码**（R-1 作废），界面 PR #4 里的「有跳过即退出码 1」必须改掉。

## R-10 A2 与 A3 不互斥：happy-path 剧本不含 HIGH，A3 走注入（`--inject high_ticket`）

- 镜鉴问：§4 硬规则「未关闭 HIGH 票不得出任务包」与 A2「一条命令跑完整轮」看似互斥。
- **裁决：不采用两段式验收，维持 A2 单命令。** 依据 R-3：mock 默认剧本只产 MEDIUM/LOW，
  HIGH 票是 A3 的**注入件**，不是 happy-path 的产物。两者从不同剧本出发，不构成互斥。
- 为了让 A3 **纯 CLI 可复现**（独立验证不该依赖 pytest 之外的私有 API），`--inject` 值域扩为：

  | 值 | 语义 | 接线方 |
  |---|---|---|
  | `none`（默认） | 不注入 | — |
  | `high_ticket` | mock 首张票升为 HIGH | 磐石：`build_llm(roles, provider="mock", high_impact=True)` |
  | `apply_fail` | 执行器应用失败（`error_category=apply_failed`） | 长流：`run_round(injection=...)` → `MockExecutor` |
  | `test_fail` | 断言失败（`error_category=test_fail`，`failed_criteria` 非空） | 同上 |

- A3 的 CLI 口径：`... round --inject high_ticket --round N --dry-run` → **退出码 3**，
  且 `tickets list` 能看到该 HIGH 票；`tickets close` 后重跑同一轮 → 正常出包。
- `--inject high_ticket` 只影响 mock 剧本，不得影响真实 provider 路径。

## R-11 A5 白名单与验收命令（原命令会误杀适配器）

- 原命令 `grep -riE "deepseek|gpt-|claude|qwen|bge" src --include=*.py` 会把 `llm.py` 适配器自己判违规，
  与铁律「适配器内部允许出现模型串」自相矛盾。
- **A5 验收命令改为两条，都必须 0 命中**：

  ```bash
  # A5.1 业务代码零模型串（排除唯一白名单文件）
  grep -rnE "deepseek|gpt-|claude|qwen|bge" src --include=*.py | grep -v "^src/kfpa/llm.py:"
  # A5.2 白名单文件自身也不许有字面量模型串（只能从 config/models.yaml 读）
  grep -nE "\"(deepseek|gpt-|claude|qwen|bge)[^\"]*\"" src/kfpa/llm.py
  ```

- 白名单**只有一个文件**：`src/kfpa/llm.py`（适配器内部）。新增白名单必须经 Chief 裁决，不许各自放宽。

## R-12 镜鉴报告里两条「main 状态」结论已过时（先 rebase 再复验）

- 已核实：`main` 现为 **`CONTRACT_VERSION = "1.1"`**（`src/kfpa/schemas.py`），
  `docs/dev/` 下 `p0-plan.md` / `p0-design-addendum.md` / `p0-rulings.md` **三份都在**。
- 镜鉴的台架若要基于最新基线，先 `git fetch origin && git rebase origin/main` 再复验；
  报告中「契约仍是 1.0」「D1 不存在」导致 A4 两条子断言 SKIP 的部分，rebase 后应转为实跑。
- `batch-close` 的文件格式**不是未定义**：见 `p0-design-addendum.md` **D1-8**
  （YAML/JSON 的 `票号 -> 结论` 映射；界面额外接受列表形状，属宽松扩展，不冲突）。

---

## 变更记录

| 版本 | 日期 | 裁决 | 作者 |
|---|---|---|---|
| 1.0 | 2026-10-16 | R-1 退出码 · R-2 运行方式 · R-3 Mock 剧本 · R-4 失败计数 · R-5 REVIEWER · R-6 票号 · R-7 异常包装与 A9 | Chief |
| 1.1 | 2026-10-16 | R-3 补 `ticket_id` 去重口径 · R-8 `--impact` 下推 Store · R-9 batch-close 退出码（禁用 1） | Chief |
| 1.2 | 2026-10-16 | R-10 A2/A3 不互斥 + `--inject high_ticket` · R-11 A5 白名单与两条验收命令 · R-12 main 基线核对与 rebase 要求 | Chief |

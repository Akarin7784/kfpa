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

---

## 变更记录

| 版本 | 日期 | 裁决 | 作者 |
|---|---|---|---|
| 1.0 | 2026-10-16 | R-1 退出码 · R-2 运行方式 · R-3 Mock 剧本 · R-4 失败计数 · R-5 REVIEWER · R-6 票号 · R-7 异常包装与 A9 | Chief |

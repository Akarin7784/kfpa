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

## R-13 放行判据：只有 HIGH 阻塞，MEDIUM/LOW 不阻塞

- 明确「存在未关闭高影响票不得生成任务包」的**边界**：阻塞条件只有 `impact=HIGH 且 status=open`。
- MEDIUM / LOW 未关闭**不阻塞**出包（走 D1-1 攒批确认，保护人的确认带宽），票面照常入台账；
  任务包不因未关闭的中低票判缺段。
- 放行时 `rounds.state → RELEASED`；被拦停时（有未关闭 HIGH）`→ TICKETS_OPEN`，CLI 退出码 3。
- 依据：design.md §4 硬规则写的是「未关闭的**高影响**質問票」，不得扩大解释。

## R-14 LangGraph state 契约与 `build_graph()` 签名（冻结，回应 D1-12 未定义）

```python
# src/kfpa/pipeline.py（长流）
class RoundGraphState(TypedDict, total=False):
    # 入参（invoke 时必须给全）
    round_no: int
    design_text: str
    design_ref: str
    # 图内逐步填充
    state: RoundState
    tickets: list[QATicket]
    open_high: list[str]
    task_package: TaskPackage | None
    exec_result: ExecResult | None
    gate: GateReport | None
    snapshot: Snapshot | None
    attempt: int
    avoid_patterns: list[str]
    message: str

GRAPH_NODES = ("gen_tickets", "guard_tickets", "build_package", "execute", "gate", "finalize")

def build_graph(*, store, llm, executor, workdir=".", max_attempts=3,
                dry_run=True, injection="none", checkpointer=None): ...
```

- `invoke` 入参**最少**给 `{"round_no", "design_text", "design_ref"}` 三键，其余由图内节点填充。
- **终止态必须含**：`state / tickets / task_package / exec_result / gate / snapshot / message`（类型同 `RoundResult` 字段）。
- 边：`gen_tickets → guard_tickets`；若有未关闭 HIGH → 直接 `finalize`（`state=TICKETS_OPEN`，退出码 3），
  否则 `build_package → execute → gate`；`gate` 判 NEEDS_FIX 且 `attempt < max_attempts` → 带 `avoid_patterns` 回 `execute`；
  `PASS` / `BLOCKED` → `finalize`。
- `run_round()` 是 `build_graph(...).invoke(init)` 的薄包装（返回 `RoundResult`），业务判据仍在纯函数里。
- **A8 断言（环境无关）**：图可编译；`invoke` 返回 dict 且 `round_no == 1`、`tickets` 非空、
  `task_package is not None`、`gate.state == "pass"`。**不断言 `snapshot`**（依赖 git 标签环境）。

## R-15 界面两个小口径 + R-8 兼容 shim 处置

- 单张 `kfpa tickets close` 遇已关闭票 → **幂等忽略 + 退出码 0** + 打印原因（与 R-9 一致）。
- `kfpa tickets batch-close --file` 的**文件不存在** → 退出码 **4**（同「设计文件不存在」）。
- 界面为等 R-8 写的**兼容 shim 允许在本分支暂存，但合并进 main 前必须删除**：
  磐石 PR #3 带上 `impact` 参数后，界面 rebase → 去 shim → 复跑自测（43/43 + 集成 8/8）再合。

## R-16 集成预演纪律：预演不上推 main

- 四条分支合并做全链预演，只允许在**本地临时分支或临时工作树**里进行，
  **禁止 `git push` 到 `origin/main`**（未评审的代码不得进主干）。
- 正式合入顺序由 Chief 按 PR 走：`#3 core（含 R-8）→ pipeline → #4 ui → #6 verify`；
  每合一个，镜鉴在 main 上复跑 A1–A9。
- 预演发现冲突/接口不一致 → 群里报，不许自行改 main 或别人的分支。

## R-17 marker 规范：`KFPA_TASK=` 为唯一规范，`[KFPA:…]` 永久保留为兼容别名

- 背景：D1-7 写的是 `[KFPA:GEN_TICKETS]` / `[KFPA:BUILD_PACKAGE]` / `[KFPA:REVIEW]`，
  R-3 写的是 `KFPA_TASK=qa_tickets` / `KFPA_TASK=task_package` —— 两个字面量并存，
  而长流已按 R-3 往 prompt 里写，磐石用别名把两者都兼容了（做法正确）。
- **裁决：以 R-3 的 `KFPA_TASK=<任务名>` 为唯一规范**（它是 k=v 形式，便于将来加参数、便于 grep）。
  D1-7 的 `[KFPA:…]` **降为兼容别名，永久保留**。
- **禁止**在代码注释里写「过渡期兼容，将来删除」这类话：将来谁照注释删掉别名，
  就会在运行期静默炸成 `MockScriptError`。别名与规范 marker 都必须有测试覆盖。
- 连带更新：`p0-design-addendum.md` D1-7 已加口径标注。

## R-18 中文输出在非 UTF-8 控制台/重定向下必须不崩（Chief 实测抓到的产品缺陷）

**现象（我在本地集成树上实测，`main + p0/core + p0/ui + p0/verify`）**：

```
$ python -c "import sys; print(sys.stdout.encoding, sys.stdout.isatty())"
cp1252 False                      # stdout 被重定向时 Python 用 locale 编码

$ python -m kfpa --help           → UnicodeEncodeError → rc=1   （应为 0）
$ python -m kfpa init --db it.db  → UnicodeEncodeError → rc=1   （应为 0）
$ python -m kfpa tickets list ... → UnicodeEncodeError → rc=1   （应为 0）
$ $env:PYTHONIOENCODING="utf-8" 后重跑：rc=0 / 0 / 0，退出码全对
```

**影响面**：这是**产品缺陷**，不是环境噪音 —— 任何把输出重定向到文件/管道的用法都会炸：
`--help`、`init`、`tickets *`、`round`（中文阶段日志）、`--json`（`dumps` 是 `ensure_ascii=False`，中文原样输出）。
CI/验收台架只要捕获输出，就会把**全绿判成全红**（或反过来，让没捕获输出的台架给出假绿）。

**裁决 R-18**：

1. **代码兜底（必须做，归磐石）**：`cli.main()` 入口处把 `sys.stdout` / `sys.stderr` 重新配置为 UTF-8：

   ```python
   for _stream in (sys.stdout, sys.stderr):
       try:
           _stream.reconfigure(encoding="utf-8", errors="replace")
       except (AttributeError, ValueError, OSError):
           pass
   ```

   放在 `main()` 最前、`parse_args` 之前（`--help` 也要被覆盖）；`__main__.py` 与 console_scripts 两条入口都过 `main()`，一处即够。
2. **运行约定补充（R-2b）**：验收与 CI 命令统一加 `$env:PYTHONIOENCODING="utf-8"`（bash: `PYTHONIOENCODING=utf-8`）。
   代码兜底修好后这条是双保险，不做也可以跑绿；但**台架必须至少有一遍不设该变量**，否则测不出这类缺陷。
3. **新增验收 A10**：在 stdout 被重定向（非 UTF-8）且**不设** `PYTHONIOENCODING` 的条件下，
   `kfpa --help` / `kfpa init` / `kfpa tickets list` / `kfpa round --json` 均不得因编码崩溃（无 `UnicodeEncodeError`，rc 符合 D1-5）。
4. **对台架的要求**：镜鉴的 A1/A7 此前报 `rc=0`，说明台架跑的时候吞掉了这层（设了变量或换了解码）。
   请在不设 `PYTHONIOENCODING` 的条件下补跑 A1/A7/A10——这类「环境相关的假绿」比功能缺陷更危险。

## R-19 R-18 的兜底必须覆盖独立脚本（台架自己也要能跑）

**现象（我在合并后的 main 上跑最终验收时抓到）**：`cli.main()` 补上 R-18 兜底后 CLI 四条命令全绿，但

```
$ python scripts/verify_p0.py
  File "…\Lib\encodings\cp1252.py", line 19, in encode
UnicodeEncodeError: 'charmap' codec can't encode characters in position 8-11   → rc=1
```

**根因**：`scripts/**` 是独立可执行脚本，**不经过 `cli.main()`**，拿不到那层兜底。

**裁决 R-19**：

1. `scripts/**` 下每个可直接执行的脚本，必须在 `main()` 最前做与 R-18.1 等价的 stdio 兜底
   （`sys.stdout` / `sys.stderr` 都 `reconfigure(encoding="utf-8", errors="replace")`，try/except 包住）；
   可以各自实现，也可以集中到 `scripts/_stdio.py` 供 import。
2. 归属：`scripts/**` 归镜鉴。修完必须贴一条**不设 `PYTHONIOENCODING`** 的复现证据
   （`python scripts/verify_p0.py *> out.txt; $LASTEXITCODE`）。
3. 同样适用于将来任何新增的独立脚本（`scripts/*.py`、运维小工具）。
   验收口径：**clean env（不设 `PYTHONIOENCODING`、stdout 重定向）下 rc 符合预期且输出无 `UnicodeEncodeError`**。
4. 测退出码的姿势统一为「先重定向到文件再读 `$LASTEXITCODE`」：PowerShell 里
   `python … | Select-Object -First 1` 会掐断上游，把 rc 污染成 0（我踩过，差点把未知票号读成 0）。

## R-20 契约升版必须同批同步「升版守卫」，由 Chief 在升版 PR 里直接改

- 台架里有一条**故意的守卫**：`scripts/verify_p0.py` 的 `SUPPORTED_CONTRACT_VERSIONS` 与
  `tests/test_contract.py` 的 `SUPPORTED_CONTRACT_VERSIONS` 不含当前版本时，A1 与 pytest 直接红，
  文案是「升版必须同步 scripts/verify_p0.py 与 docs/acceptance/」。这是好设计，保留。
- **裁决**：升版由 Chief 发起，Chief 必须在**同一个 PR**里同步三处，不许把 main 留在红状态：
  1. `src/kfpa/schemas.py` 的 `CONTRACT_VERSION`（＋本文件顶部的变更记录）
  2. `scripts/verify_p0.py`：`CONTRACT_VERSION_LATEST` + `SUPPORTED_CONTRACT_VERSIONS`
  3. `tests/test_contract.py`：`SUPPORTED_CONTRACT_VERSIONS`
- 这三处里 2/3 落在镜鉴的写者范围内：属于**升版守卫的同步义务**，Chief 越界修改时必须在 PR 里明说，
  由镜鉴复验（owner 不变，只是本节把「谁改」定死，避免升版卡住）。
- 升版 PR 的验收命令固定为：`python scripts/verify_p0.py` + `python -m pytest tests -q` + `python scripts/selfcheck_core.py`，
  三条都必须 rc=0（clean env）。
- 首次执行记录：契约 1.0→1.1（守卫就位）、1.1→**1.2**（P1 D3-3.1，本 PR）。

---

## 变更记录

| 版本 | 日期 | 裁决 | 作者 |
|---|---|---|---|
| 1.0 | 2026-10-16 | R-1 退出码 · R-2 运行方式 · R-3 Mock 剧本 · R-4 失败计数 · R-5 REVIEWER · R-6 票号 · R-7 异常包装与 A9 | Chief |
| 1.1 | 2026-10-16 | R-3 补 `ticket_id` 去重口径 · R-8 `--impact` 下推 Store · R-9 batch-close 退出码（禁用 1） | Chief |
| 1.2 | 2026-10-16 | R-10 A2/A3 不互斥 + `--inject high_ticket` · R-11 A5 白名单与两条验收命令 · R-12 main 基线核对与 rebase 要求 | Chief |
| 1.3 | 2026-10-16 | R-13 放行判据（仅 HIGH 阻塞）· R-14 LangGraph state 契约与 `build_graph()` 签名 · R-15 界面小口径与 shim 处置 · R-16 集成预演纪律 | Chief |
| 1.4 | 2026-10-16 | R-17 marker 规范（`KFPA_TASK=` 为准，`[KFPA:…]` 永久兼容别名）· D1-7 加口径标注 | Chief |
| 1.5 | 2026-10-16 | R-18 中文输出在非 UTF-8 控制台/重定向下必须不崩（新增验收 A10；R-2b 运行约定补充） | Chief |
| 1.6 | 2026-10-16 | R-19 R-18 兜底必须覆盖独立脚本（`scripts/**` 自己也要能在 clean env 下跑）| Chief |
| 1.7 | 2026-10-16 | R-20 契约升版必须同批同步「升版守卫」（Chief 直改三处 + 镜鉴复验）| Chief |

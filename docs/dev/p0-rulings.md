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

## R-21 升级守卫的同步义务归「发起变更者」；schema_version 不许在台架里写死

- 磐石预告：D3 要求 `meta.schema_version = "2"`，而 `scripts/selfcheck_core.py` 有四处把 `"1"` 写死
  （52/53/54/122 行），升版后必红。这是**预期内的守卫反应**，但同步义务要定清楚。
- **裁决**：
  1. **同步义务归「发起变更的人」**（R-20 的一般化）：谁升版，谁就在**同一个 PR** 里把受影响的守卫一并改掉，
     越界修改必须在 PR 描述里明说，由该文件 owner 复验。`schema_version 1→2` → 磐石（`p1/store`）；
     `CONTRACT_VERSION 1.1→1.2` → Chief（已按 R-20 执行，镜鉴已复验）。
  2. **`schema_version` 与 `CONTRACT_VERSION` 口径不同，别混**：
     - `CONTRACT_VERSION` 的守卫**故意写死**，逼你同步（升版不同步就红，这是特性）；
     - `schema_version` 的断言**必须跟常量走** —— `scripts/selfcheck_core.py` 里那四处字面量
       改成从 `kfpa.store` 导入的 `SCHEMA_VERSION`（含 `set_meta` 幂等检查）；
       这样 2→3 不需要再改台架，也不会有「改了 store 忘了台架」的第二次踩坑。
  3. **表常量语义**（照准磐石方案，命名不骗人）：
     `BUSINESS_TABLES` / `TABLE_NAMES` **保留 P0 含义不变**（meta + 六表）；
     新增 `P1_TABLE_NAMES = ("ledger", "knowledge")` 与 `ALL_TABLE_NAMES = TABLE_NAMES + P1_TABLE_NAMES`。
     需要「当前所有表」的断言（A12 双库对拍、P1 台架）一律用 `ALL_TABLE_NAMES`（9 张）。

## R-22 P1 首次全量验收的六个红：两个集成根因 + 四条口径

**实测（我合并 #22/#19/#20/#18/#21 后，在 main `3596f6c` clean env 跑 `verify_p1.py`）**：
`A11/A12/A13/A15/A16/A17` FAIL，`A14` PASS。逐条定位到四个根因：

| 根因 | 影响断言 | 归属 |
|---|---|---|
| `cli.py` 没挂载 `ledger` / `knowledge` / `fastlane` 三个子命令（`--help` 里只有 init/round/gate/history/rollback/tickets） | A11 / A15 / A16 | 磐石（CLI）+ 长流（`register()`） |
| `docker-compose.pg.yml` 默认宿主端口 55432 被别的项目容器占用（`wms-pg` / `wms-pg-verify` / `wms-pg-base`） | A12 | 磐石 |
| pipeline 默认没用 `SqliteSaver`，workdir 下没有 checkpoint 库 | A13 | 长流 |
| A17 的调用口径未对齐 | A17 | 界面 + 镜鉴 |

**裁决**：

1. **PG 宿主端口默认改 `55440`**，支持 `KFPA_PG_PORT` 覆盖（compose：`ports: "${KFPA_PG_PORT:-55440}:5432"`）。
   本机 55432/55433/55434 已被其它项目占用，**KFPA 不得占用这三个端口**；台架 A12 优先读 `KFPA_PG_PORT`。
2. **checkpoint 落盘口径（冻结）**：P1 默认 `SqliteSaver`，库固定为 **`{workdir}/checkpoints.sqlite`**，
   `thread_id = f"kfpa-r{round_no}"`，可用 `--checkpoint-db` 覆盖；
   台架 A13 按此对齐搜索目录与文件名（现已在 workdir 找 `*.sqlite`，形状兼容）。
3. **G7 的启用条件**：**只有显式提供覆盖率报告时才启用**（`kfpa round --coverage-json <cov.json>`）；
   无报告 → G7 跳过，**不得判红**（否则默认 happy-path 全红）。json 顶层需含 `critical_paths_ok`
   （由台架把「A1–A10 是否全绿」写进去）；**缺该键即 False，不许编造**。
4. **覆盖率门槛只统计核心模块白名单**：`schemas.py`、`config.py`、`cli.py`、`store/**`、`gates.py`、`pipeline.py`；
   **不含** `ui/**`、`executor.py`（适配器）、`scripts/**`。全仓库行覆盖不作为门槛。
5. **A14 本轮只验机制**：提供报告 → 正确判 `NEEDS_FIX` + `error_category=coverage` + 未达标文件清单即通过；
   真实达标（白名单行覆盖 ≥80%）登记为 **P1 尾项债务**，不阻塞本轮合并（当前全仓库 37.6%）。
6. **A17 以 `scripts/verify_p1.py` 的调用口径为准**（验收方定义接口），界面按其对齐；
   有歧义找镜鉴澄清，不许各自「就地发明」入口。

**派工**：磐石补 CLI 接线（三个子命令 + `--coverage-json` + `--checkpoint-db`）与 compose 端口；
长流落 checkpoint 默认盘并提供 `register()`；界面对齐 A17；镜鉴按 1–3 条调整台架后复跑。

## R-23 快车道承认不进 P1；`diff_files` 的三来源优先级（补 D3 两处空白）

- 界面问的两处 D3 空白：①快车道承认的落库接口；②`impact.analyze()` 的 `diff_files` 从哪来。
- **裁决 1：快车道「承认」不落库，P1 不做。**
  - 依据路线图：design.md §8 把「快车道 + 沉淀闸门**全量**落地」放在 **P3**；P1 只交付**判据计算**与
    `kfpa fastlane check --round N`（A16 的全部内容）。
  - 因此 P1 **不新增** `fastlane_requests` 表、不动 `schema_version`、不升契约。
  - 界面的快车道页只能做**预览与提示**：结论、三判据、边界清单都可展示；
    任何「承认/放行」按钮的文案必须写明 **「预览：P3 才接攒批承认入库」**，禁止做出已持久化的假象。
  - 将来落库走 P3 契约（新增 `fastlane_requests` 表 + `schema_version` 递增，按 R-21 同批同步守卫）。
- **裁决 2：`diff_files` 三来源优先级**（`impact.analyze(task_package, diff_files)`）：

  | 优先级 | 来源 | 适用 |
  |---|---|---|
  | 1 | `ExecResult.files_changed`（本轮执行器真实产出） | `kfpa fastlane check --round N` 默认路径 |
  | 2 | CLI 显式传参 `--files a.py,b.py`（逗号分隔） | 无执行结果时的预判 |
  | 3 | 都拿不到 → **`fast_lane=None` + `reason="no_diff_source"`** | 不猜、不默认 True |

  - 界面预览可用 `git diff --name-only HEAD` 作**展示层**来源，但 **CLI 判定不得依赖 git 状态**
    （脏工作树会给出假判据）。
  - 判据本身不变：不新增/改接口契约、不改数据模型、波及 ≤1 模块，三者全真才 `True`。

## R-24 P1 第二轮的三个红：账本角色口径 / compose 撞名 / fastlane 退出码

**实测（#26 合入后 main `ea74c00`，`KFPA_DB_URL` 指向已在跑的 PG）**：A11/A12/A16 FAIL，其余全 PASS。

| 现象 | 根因 | 归属 |
|---|---|---|
| A11 `NG ledger 输出含角色 FLASH` | mock 路径下 `MockExecutor` **不调 LLM**，只有 ARCHITECT 有账；台架强制 FLASH 是**断言写错** | 镜鉴 |
| A12 compose 用 55432 起栈成功但 PG 探针 rc=1 | 台架自己给 compose 传了 55432（wms-pg 占着），探针连到了别的项目的库；且 compose 固定 `container_name: kfpa-pg` 与 ad-hoc 实例撞名 | 镜鉴 + 磐石 |
| A16 `kfpa fastlane check` rc=3（期望 0） | `fastlane check` 把「无 diff 来源/需人工」映射成 3，与 D1-5 的 3（等待人工确认）语义混淆 | 长流 |

**裁决**：

1. **A11 的角色断言口径**：账本只记录**实际发生过的 LLM 调用**。mock 路径只 **REQUIRE `ARCHITECT`**；
   `FLASH` 允许缺席。要验 FLASH 记账，另加一条**直接注入用例**：`ledger.record(LedgerEntry(role="FLASH", …))`
   后 `kfpa ledger` 必须含 FLASH。禁止为了让台架变绿而让 mock 执行器凭空产生 LLM 调用。
2. **compose 不得使用固定 `container_name`**（`container_name: kfpa-pg` 删除）：固定全局名会让多个工作树、
   多个 clone、台架与人工实例互相撞名（今天就撞了）。由 Compose 按项目前缀命名（`<project>-pg-1`）。
   台架 A12 起栈时必须**显式独立项目名**（`docker compose -p kfpa-verify-a12 …`）并**优先复用**
   `KFPA_DB_URL` 已指向的 PG（有则可直接对拍，不起栈）；用完 `down -v` 只清自己那一栈。
   端口仍按 R-22.1：默认 `55440`，`KFPA_PG_PORT` 覆盖，**禁用 55432–55434**。
   禁止 `docker rm -f` 别人的容器。
3. **`kfpa fastlane check --round N` 的退出码固定为 `0`**（成功输出判定）：
   `fast_lane` 为 `True` / `False` / `None`（`no_diff_source`）都算**成功**，判定结果在输出文本与 `--json` 里。
   只有输入错误才非零（轮次/库打不开 → 5，参数错 → 2）。**不得占用 3**（3 保留给 round 的人工确认语义）。
4. **`run_round` 的覆盖率形参名冻结为 `coverage`**：
   `run_round(..., coverage: CoverageReport | None = None)`，内部转给 `gates.evaluate(..., coverage=…)`。
   CLI 现有的 `coverage` / `coverage_report` 兼容探测保留，但**以 `coverage` 为准**，将来只加这一个名字。

## R-25 存储优先级改回「显式 `--db` 优先」；台架必须剥掉 `KFPA_DB_URL`

**我自己的实测样本（合并 #27/#29 后）**：我把 `KFPA_DB_URL` 导出到全局再跑台架 →
**A6/A7/A7.2/A10/A11/A12/A13 全变红**。原因是 R-24 之后的口径是「`KFPA_DB_URL` 压过显式普通路径」，
于是台架给每个断言准备的临时库全被顶成了同一个 PG。清掉该变量重跑：P0 **13/13**、P1 **7/8**（只剩 A12）。
这不是产品 bug，但它暴露了**优先级方向错了**：显式 `--db /tmp/x.db` 被环境变量静默改写，
违反 CLI 常规约定，也制造了一整类「环境变量残留 → 台架全红」的假红。

**裁决 R-25**：

1. **优先级（改回常规）**：显式 `--db`（DSN 或普通路径） **>** `KFPA_DB_URL` **>** 默认 `kfpa.db`。
   `KFPA_DB_URL` 只在「没给 `--db`」或「`--db` 等于默认 `kfpa.db`」时生效。
   `make_store` 里 `explicit` 非空且非默认路径时，一律按 `explicit` 走，不再让 env 覆盖。
2. **台架 A12 的 PG 侧显式传 DSN**，不依赖环境变量：
   `--db postgresql://kfpa:kfpa@127.0.0.1:${KFPA_PG_PORT:-55440}/kfpa`。对拍本来就是「显式指定两库」，
   这比靠 env 隐式切换清楚得多。
3. **台架所有子进程统一剥掉 `KFPA_DB_URL`**（以及 `PYTHONIOENCODING` 的既有要求）：
   谁机器上残留了这个变量，都不许把台架的临时库顶掉。这是台架的硬要求（与 R-19/R-22 同级）。
4. A12 的端口仍按 R-22.1：台架读 `KFPA_PG_PORT`，默认 `55440`，禁用 55432–55434。

## R-26 A12 的真因：CLI 的配置查找依赖 cwd（不是 PG/DSN 问题）

**实测（我在 main `0d84690` 上手动复现）**：

```
cwd = <任意非仓库目录>
$ python -m kfpa round --design <repo>/examples/design-r1.md --round 2 --dry-run --json --workdir <tmp>/wd
[core] 角色配置错误：角色配置不存在：config\models.yaml → rc=4
$ python -m kfpa round ... --config <repo>/config/models.yaml     → rc=0（绿）
```

`init` 不需要角色表所以 rc=0；`round` 需要 `config/models.yaml`，**默认路径是相对 cwd 的字面量**
→ 任何从别的目录调用的消费者（A12 台架、CI、用户在自己项目里调）都会挂在 rc=4。
这才是 A12「PG 里没有任何表」的真因（台架把 round 判红后自然没数据），与 `KFPA_DB_URL` / DSN 无关。

**裁决 R-26**：`config/models.yaml`（以及其它随包资源，如 `prompts/*.j2`）**必须能在任意 cwd 下定位**。
解析顺序：

1. `--config` 显式路径；
2. 环境变量 `KFPA_CONFIG`；
3. cwd 及其所有父目录下的 `config/models.yaml`；
4. 包/仓库安装位置（由 `kfpa.__file__` 上溯，找到含 `config/models.yaml` 的目录）；
5. 都没有 → 中文报错列出已试过的路径 + 退出码 4（**不猜、不回退到空配置**）。

归属：磐石（`config.py` + `cli.py`）。同批做 R-25.1（`make_store` 优先级回退）。
验收：在 `$env:TEMP` 下用绝对路径跑 `python -m kfpa round …` 必须 rc=0；
A12 在独占端口上重跑应转为 PASS（不再是 SKIP）。

## R-27 A12 的原始探针必须做驱动归一（PG 元组 vs SQLite 标量）

**现象（我在独占端口 55441 上真跑 A12，R-26/R-25.1 修完后）**：两库终于都真写进去了
（9 张表齐全、`package_hash` 与快照一致），只剩一条比较失败：

```
NG A12：票号一致 SQLite=['R1-001', …] PG=[['R1-001'], ['R1-002'], …]
```

**根因**：探针走原始 SQL。SQLite 驱动的行是标量，psycopg3 的行是**元组**。
这是**台架的归一缺陷**，不是双驱动行为不一致——产品 API（`Store.list_tickets()`）两个驱动都返回
`QATicket`，磐石已实测一致。

**裁决 R-27**：

1. A12 的比较必须在**归一之后**做：两侧统一成扁平 `list[str]`（长度 1 的元组/列表取首元素、
   `None` 跳过、顺序排序）。**禁止**为了迁就探针去改产品代码。
2. 台架的 PG 探针一律显式指定 `row_factory` 或读取后手动归一，不依赖驱动的默认行形状。
3. 归一函数要有单测（喂元组 / 标量 / 混合三种输入）——这是最容易「修完这次、下次又踩」的地方。

归属：镜鉴（`scripts/**`）。改完 A12 在独占端口上应转 **PASS**，P1 即 8/8 收口。

## R-28 回灌区只取「本轮 + 上一轮」的失败，禁止全库历史

**现象（镜鉴 A12 实测）**：同一轮在两库上跑，`package_hash` 不一致——PG（共享库、已有历史）
的任务包 `feedback` 混进了 **8 条别的轮次**的 `AVOID[...]`，而全新 SQLite 库是空的。
根因：`pipeline.py` 四处调用 `store.list_avoid_patterns()` **不带轮次**，等于把全库历史灌进任务包。

**裁决 R-28**（口径来自文档原文，不是新发明）：

1. 回灌区只允许两类来源：
   - **本轮已发生的失败**（同轮重试必须回灌，design.md §5.3「失败原因必须回灌新任务包」）；
   - **上一轮（N-1）的失败**（spec.md §5.2 原文：「回灌区：**上一轮**失败原因与规避要求」）。
2. **禁止把 N-2 及更早的历史失败灌进任务包**：①违反 spec 原文；②会让 `package_hash` 随库的历史
   增长而漂移——共享库与全新库对同一轮必然给出不同 hash，A12 双库对拍变成永久假红。
3. 实现：`pipeline._scoped_avoid_patterns(store, round_no)` 取 `round_no` 与 `round_no - 1` 两轮、
   去重保序；`_stage_package` / `n_build_package` / `n_gate` / `n_finalize` 四处统一走它。
4. 台架不需为此改断言：修完后同一轮在「空库」与「有历史库」上应给出**相同** `package_hash`。

归属：长流（`pipeline.py`）。本次由 Chief 代推（长流连续两轮未动，且这是 A12 剩余两处阻塞之一）。

## R-29 升版守卫的同步范围 = 全部台架；新增台架必须自带版本断言

- 镜鉴 A0 抓到：契约升 1.3 时只同步了 `verify_p0.py` 与 `tests/test_contract.py`，
  **`verify_p1.py` 还锁在 1.2**（`SUPPORTED_CONTRACT_VERSIONS = ("1.2",)`）→ 升版后 P1 台架必红。
  这是 R-20 的**范围写窄了**：当时只点了「三处」，没写「所有台架」。
- **裁决**：
  1. 契约升版（`CONTRACT_VERSION`）时，必须同批同步**仓库里每一个** `scripts/verify_*.py`
     的 `CONTRACT_VERSION_LATEST` 与 `SUPPORTED_CONTRACT_VERSIONS`，以及 `tests/**` 的同类清单。
     漏一个都算升版未完成（R-20/R-21 的一般化）。查漏：
     `grep -rn "SUPPORTED_CONTRACT_VERSIONS" scripts tests` —— 每处都必须含新版本号。
  2. **新增台架必须自带**当前契约版本的断言（新写 `verify_p2.py` 等要包含 `SUPPORTED_CONTRACT_VERSIONS`），
     否则它逃过升版守卫。
  3. 允许存在**额外诊断字段**（如 `samples` 里的 `parse_ok`/`context_id`），但**消费方必须容忍未知键**；
     冻结的只是「最小必需键集」（P2 报告：顶层六键 + `samples` 的 `missed`/`false_alarm` 布尔）。
     生产方可以加诊断字段，不得删必需键。

## R-30 P2 测试归属：实现方的桩落在自己范围内，由镜鉴搬进 `tests/**`

- 背景：D4 §3.5 的覆盖率债要求 `review.py`/`eval.py` 白名单行覆盖 ≥80%，而 A20 是在 main 上跑
  `pytest --cov=kfpa`。长流的桩（54 项）只在他的临时脚本里 → main 上这两个模块接近 0 覆盖，A20 必红。
- **裁决**（沿用 P0 先例：磐石自检脚本由镜鉴搬进 `scripts/`）：
  1. **`tests/**` 仍归镜鉴单写**，实现方**不越界**加 `tests/test_p2_*.py`。
  2. **交接路径**：长流把桩落成**他自己写者范围内的文件** —— `benchmarks/review/stub_p2_review.py`
     （避免在群里贴几千行）；镜鉴把它搬进 `tests/test_p2_review.py`，并按独立验证口径改写/复核断言
     （保留边界用例：空集 / 全漏 / 全错 / 缺 `baseline_*` / 非法 JSONL / `--out` 与 rc=4 分支）。
  3. **覆盖率口径**：临时脚本的数字**不能替代** A20；必须由仓库内 `tests/**` 撑起白名单覆盖，
     由台架生成合并 `coverage.json` 交给 G7 判定。
  4. 交接未完成前 A20 判 **FAIL**（可注明「待交接：`benchmarks/review/stub_p2_review.py` → `tests/`」），
     **不许静默 SKIP**。
  5. 同一路径适用于后续所有「实现方自测 → 仓库测试」的搬运。

## R-31 A18 的 rc 口径纠正 + A20 覆盖率缺口的真实拆解（不放水）

**P2 首轮全量（main `7c1cabc`，clean env，独占 PG 端口）**：`verify_p2` **A0/A19/A21/A22 PASS，
A18/A20 FAIL**。

### 一、A18：未知 provider 属**用法错误 → rc=2**（D4 §3.2 我写错了）

实测 `kfpa round --provider bogus …` 由 argparse `choices` 原生拒绝，**rc=2**；D4 §3.2 却写「未知 provider → rc=4」，
台架按 D4 判红。D1-5 里 `2 = 用法/参数错误`、`4 = 依赖/环境错误`——**产品行为是对的，我的 D4 条款错了**。

**裁决（rc 三分）**：

| 情形 | rc | 依据 |
|---|---|---|
| `--provider` 值不在 choices 内 | **2** | D1-5：用法/参数错误（argparse 原生，不劫持） |
| provider 合法，但运行期依赖/凭据缺失（litellm 未装、无 key、模型名错 → RuntimeError/ImportError） | **4** | D1-5：依赖/环境错误 |
| I/O 与数据库错误 | **5** | D1-5 不变 |

- D4 §3.2 的「未知 provider → rc=4」**作废**，按本表读。
- 归属：磐石把适配器抛出的依赖/凭据类 `RuntimeError` 映射到 **4**（当前落成 5）；
  镜鉴把 A18 期望改成「未知值 → 2 + 中文；合法但缺 key → 4」。

### 二、A20：白名单 67.5%，缺口真实且可拆（不放水，保持 ≥80%）

实测（`pytest --cov=kfpa`，白名单 1957 语句）：

| 文件 | 覆盖率 | 未覆盖语句 |
|---|---|---|
| `store/postgres.py` | **0.0%** | **196**（最大单点：pytest 里从没跑过 PG 路径） |
| `gates.py` | 35.8% | 129 |
| `store/__init__.py` | 56.2% | 21 |
| `cli.py` | 67.8% | 104 |
| `pipeline.py` | 76.4% | 105 |
| `store/sqlite.py` | 77.7% | 46 |
| `config.py` / `eval.py` / `review.py` / `schemas.py` | 85.6 / 89.8 / 90.2 / 99.6% | 13 / 11 / 10 / 1 |
| **白名单合计** | **67.5%** | 差 **+245** 语句才到 80% |

**裁决**：门槛**不放水**（白名单行覆盖 ≥80% 且关键路径全绿）；缺口按下表归属补齐。
无 Docker 时 PG 用例可 `skip`，但 **skip 不计入覆盖**，验收必须在有 Docker 的机器上跑一遍（本机有）。

| 归属 | 目标 |
|---|---|
| 磐石 | `store/postgres.py` 0→≥80%（PG 测试夹具/端口约定）、`store/__init__.py`、`store/sqlite.py` |
| 长流 | `gates.py` 35.8→≥80%（G1–G8 边界分支）、`pipeline.py` 76.4→≥80% |
| 界面 | `cli.py` 的 `tickets` / 页面分支（与磐石分列不同函数） |
| 镜鉴 | `tests/**` 落地（R-30 交接路径）、`verify_p2 --coverage-out` 复核、A18 期望改口径 |

## R-32 A20 白名单拆成「纯逻辑」与「需外部服务」两类；PG 驱动的覆盖交给 A12

- 镜鉴问：`store/postgres.py`（184 行）在 **clean env（无 Docker）下永远 0%**，而 A20 就是 clean env 跑的
  → 门槛到不了 80%。这是**我的白名单设计问题**：R-22.4 把 `store/**` 整包划进门槛，没区分
  「纯逻辑」与「需要外部服务」。
- **裁决**：
  1. **A20 门槛（clean env 必须能跑）只算纯逻辑白名单**：
     `schemas.py`、`config.py`、`cli.py`、`gates.py`、`pipeline.py`、`review.py`、`eval.py`、
     `store/__init__.py`、`store/sqlite.py`。
     **`store/postgres.py` 从 A20 门槛排除**（与 `ui/**`、`executor.py`、`scripts/**` 同理）。
  2. **PG 驱动的覆盖由 A12 双库对拍保证**（有 Docker 时真跑：9 表、票号、账本、门禁态、
     `package_hash` 全一致），集成级验收比逐行覆盖更贴近风险。
  3. 新增**可选报告项 A20b**：有 Docker 时输出 `store/postgres.py` 覆盖率与行数，**只报告、不作门槛**；
     无 Docker 时显式 `SKIP`（可见，不算通过）。
  4. 重算门槛：纯逻辑白名单 1761 语句，当前 1321（**75.0%**），**还需 +88 语句**到 80%
     —— 主要在 `gates.py`（129 行未覆盖）与 `pipeline.py`（105 行未覆盖）。
- 归属不变（R-31）：长流 `gates.py`/`pipeline.py`；磐石 `store/sqlite.py`、`store/__init__.py`
  与「适配器依赖类 `RuntimeError` → rc=4」；界面 `cli.py` 的 `tickets`/页面分支；
  镜鉴 `tests/**` 落地（R-30 交接路径）+ 台架 A18 期望改 rc 三分 + A20/A20b 口径。

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
| 1.8 | 2026-10-16 | R-21 升级守卫同步义务归发起变更者 · `schema_version` 断言跟常量走 · `ALL_TABLE_NAMES` 语义 | Chief |
| 1.9 | 2026-10-16 | R-22 P1 首轮验收六红定位：PG 端口默认 55440 · checkpoint 落盘口径 · G7 仅在提供报告时启用 · 覆盖率白名单 · A14 只验机制 · A17 以台架口径为准 | Chief |
| 2.0 | 2026-10-16 | R-23 快车道承认不进 P1（P3 落库）；`diff_files` 三来源优先级（执行产物 → `--files` → `no_diff_source`） | Chief |
| 2.1 | 2026-10-16 | R-24 账本角色断言只认实际调用 · compose 去固定 `container_name` + 台架独立项目名 · `fastlane check` 退出码 0 · `run_round(coverage=)` 形参名冻结 | Chief |
| 2.2 | 2026-10-16 | R-25 存储优先级改回「显式 `--db` 优先于 `KFPA_DB_URL`」· 台架 A12 显式传 DSN · 台架子进程必须剥掉 `KFPA_DB_URL` | Chief |
| 2.3 | 2026-10-16 | R-26 `config/models.yaml` 等随包资源必须能在任意 cwd 定位（A12 的真因）；归属磐石 | Chief |
| 2.4 | 2026-10-16 | R-27 A12 探针必须做驱动归一（PG 元组 vs SQLite 标量），禁止改产品代码迁就探针 | Chief |
| 2.5 | 2026-10-16 | R-28 回灌区只取「本轮 + 上一轮」失败（spec §5.2 原文），禁止全库历史导致 `package_hash` 漂移 | Chief |
| 2.6 | 2026-10-17 | R-29 升版守卫=全部台架 · R-30 测试归属与交接路径 · R-31 A18 rc 口径纠正（未知 provider=2）+ A20 覆盖率缺口拆解（67.5%，不放水） | Chief |
| 2.7 | 2026-10-17 | R-32 A20 白名单拆分：排除需外部服务的 `store/postgres.py`（覆盖交给 A12），纯逻辑门槛重算 75.0%，还需 +88 语句 | Chief |

# KFPA P0 独立验收报告

> 负责人：**镜鉴**（分支 `p0/verify`，工作树 `workspace\kfpa-jingjian`）
> 依据（效力从高到低）：`docs/dev/p0-rulings.md`（D2）→ `docs/dev/p0-design-addendum.md`（D1，契约 1.1）
> → `docs/dev/p0-plan.md` §4 验收标准 A1–A9。本报告只做**独立验证**，不改任何人写的实现。
> 结论必须在「命令 + 真实输出片段 + 结论」三件套齐备的前提下给出，禁止只写「已通过」。

---

## 0. 当前状态

| 项 | 值 |
|---|---|
| 验收台架 | `scripts/verify_p0.py`（本分支交付，A1–A9 共 12 条断言） |
| 契约版本 | main 已升 `CONTRACT_VERSION 1.1`（PR #2）＋跨模块裁决 D2（PR #5） |
| 最近一次全量运行 | 2026-10-16，基于 `main@201e517` 的台架自检：**10 条断言 → PASS 1 / FAIL 9 / SKIP 0** |
| 那一条 PASS | `A5` 模型字符串铁律（main 基线本来就干净） |
| 判定 | **P0 未通过**：`p0/core`、`p0/pipeline`、`p0/ui` 尚未交付，FAIL 全部指名缺失文件与责任模块 |

台架**不给未交付模块发通过**：契约文件缺失一律报 FAIL 并列出「缺哪个文件、归谁写」；
`A8` 即使 pytest 返回 0，只要冒烟被 skip 也判 **FAIL**（skip 不等于通过）。

---

## 1. 怎么跑（离线，无网络、无 API key）

R-2 规定统一 `PYTHONPATH=src`、不做 editable 安装（四个 clone 会互相顶掉 site-packages）。

```powershell
$env:PYTHONPATH="src"; python scripts/verify_p0.py            # 全量；0=无 FAIL / 1=有 FAIL / 2=用法错
$env:PYTHONPATH="src"; python scripts/verify_p0.py --only A4,A8
$env:PYTHONPATH="src"; python scripts/verify_p0.py --json out.json
$env:PYTHONPATH="src"; python scripts/verify_p0.py --list
$env:PYTHONPATH="src"; python -m pytest -q                    # 契约测试 + 台架自检 + A8 冒烟
```

```bash
PYTHONPATH=src python scripts/verify_p0.py --json out.json
```

断言之间独立：每条各自建临时 SQLite 与临时 workdir，不依赖执行顺序。
除 `--dry-run` 外不依赖网络、API key、aider/streamlit；langgraph/jinja2/pyyaml/pytest 已由 Chief 装齐。

---

## 2. A1–A9 逐条结论

| 台架 id | 验收项 | 责任模块 | 当前结论 |
|---|---|---|---|
| A1 | `--help` 退出码 0，含 `init/round/tickets/history` **+ R-1 退出码表** | 磐石 + 界面 | **FAIL**（缺 `cli.py`/`__main__.py`/`ui/cli.py`） |
| A2 | dry-run 整轮：票五要素 + D1-1 票号规则、任务包六段、`exec_result.results`、D1-9 六表落库 | 磐石 + 长流 | **FAIL**（缺 `store.py`/`pipeline.py`/`examples/design-r1.md`） |
| A3 | 未关闭 HIGH 票 → **退出码 3** + 中文；关票后放行；R-6 同票号重跑保留 status/answer | 长流 + 界面 | **FAIL**（缺 `pipeline.py`/`store.py`） |
| A4 | D1-3 的 G1–G6 判据 + D1-4/R-4 同类第 2 次失败即 `BLOCKED`/`escalated` + D1-11 `AVOID[...]` 回灌 | 长流 | **FAIL**（缺 `gates.py`） |
| A5.1 | R-11.1：`src/` 业务代码零模型串（唯一白名单 `src/kfpa/llm.py` 除外） | 全员 | **PASS**（命中 0 处） |
| A5.2 | R-11.2：白名单文件自身零字面量模型串（只能从 `config/models.yaml` 读） | 磐石 | **PASS**（命中 0 处） |
| A5.3 | 4 角色、MockLLM 确定性（D1-7）、非法 role 拒绝、`high_impact=True` 开关（R-3/R-10） | 磐石 | **FAIL**（`high_impact` 未实现：`TypeError: MockLLM.__init__() got an unexpected keyword argument 'high_impact'`） |
| A6 | PASS 轮落 `snapshots` + tag `kfpa-r{n}`（D1-6）+ `history` 列出 | 磐石 | **FAIL**（缺 `store.py`/`cli.py`） |
| A7 | 台账列全（D1-8 七列）/过滤/`close` 落库/`batch-close` 映射 + 未知票号**整体不写**(退出码 2) | 界面 | **集成树实测 PASS**（界面侧全绿；见 §4-1） |
| A7.2 | R-8 `impact` 下推 Store ＋ R-9 重复提交幂等(0)/空答案事务(2) | 磐石 + 界面 | **FAIL**（R-8 未落地：`Store.list_tickets` 只有 `(round_no, status)`；R-9 侧全绿） |
| A8 | LangGraph 图冒烟（`build_graph` 可编译可跑，D1-12）＋纯函数核心仍有保险丝 | 长流 | **FAIL**（`build_graph` 未交付，冒烟 skip → 判 FAIL） |
| A9 | LiteLLM 适配器把底层异常统一包成中文 `RuntimeError` 且提示 `--provider mock`（R-7） | 磐石 | **集成树实测 PASS**（消息：`模型调用失败（角色 FLASH，provider=litellm）：… → P0 验收请用 --provider mock`） |

模块交付后本表逐行回填「命令 + 输出片段」。

### 版本门控

main 已是 1.1，全部门禁字段（`results`/`error_category`/`reason_key`/`escalated`/`same_reason_count`）都可断言。
若某分支仍落 `1.0`，A1 会给「该分支未 rebase 到 main」的建议，A4 的相关子断言报 SKIP —— 这不算通过。

---

## 3. 台架自检（先证伪验收工具本身）

`tests/test_verifier_selfcheck.py` 用**故意残缺/故意投毒**的桩仓库跑台架：

| 自检 | 期望 | 结果 |
|---|---|---|
| 桩仓库只有 `src/kfpa/__init__.py`，跑 A1 | FAIL，诊断指名 `src/kfpa/cli.py` + 责任模块 | 通过 |
| 桩仓库 `src/kfpa/oops.py` 放 `deepseek/...`，跑 A5 | FAIL，命中文件被点出 | 通过 |
| 桩仓库 `src/kfpa/llm.py` 放 `deepseek/...`，跑 A5 | PASS（白名单不误杀） | 通过 |
| 桩仓库 `CONTRACT_VERSION="9.9"`，跑 A1 | 诊断里出现 `9.9` | 通过 |
| `--only NOPE` | 退出码 2（而非静默通过） | 通过 |
| 真仓库跑 A5.1 | PASS | 通过 |

`tests/test_contract.py` 对契约 1.1 做独立复算（枚举、票面五要素、六段判定、`package_hash`
确定性、`dumps` 稳定性、v1.0/v1.1 字段门控）。

实测：`PYTHONPATH=src python -m pytest tests -q` → **30 passed, 2 skipped**（2 skip = A8 冒烟在
`build_graph` 交付前 skip，台架已把它判 FAIL）。

---

## 4. 独立复核发现

### 4.1 PR #3（p0/core @95daedd）× PR #4（p0/ui @c124202）集成实测

集成树 = `main@6689c1f` + `p0/core` + `p0/ui` + `p0/verify`（`git merge` 三方无冲突）。

| 发现 | 证据 | 级别 |
|---|---|---|
| **R-8 未落地**：`Store.list_tickets` 只有 `(round_no, status)`，界面按 R-8 传 `impact=` 后 `kfpa tickets list`（含不带 `--impact` 的普通调用）**整条 rc=5** | `TypeError: Store.list_tickets() got an unexpected keyword argument 'impact'` → `[kfpa-ui] 读取台账失败…` | 阻塞（磐石） |
| **R-10 未落地**：`--inject` 值域缺 `high_ticket`；`build_llm(..., high_impact=True)` 抛 TypeError | A3 实测 `rc=2`（argparse 拒绝）；`TypeError: MockLLM.__init__() got an unexpected keyword argument 'high_impact'` | 阻塞（磐石） |
| **R-1 退出码偏差**：设计文件不存在 → 5，R-1 表定为 4 | `round --design nope.md` → `[core] 设计文件不存在：nope.md` `rc=5` | 重要（磐石） |
| **界面侧全绿**（临时补 R-8 桩隔离验证，验后还原）：A7 列全/三过滤/close 落库/batch-close 映射与未知票号事务全部符合；R-9 三条（重复→0 幂等、空答案→2 整体不写、无退出码 1）全部符合 | 见 PR #4 评论 | — |
| **A9 通过**：适配器异常统一包成中文 RuntimeError 且提示 `--provider mock` | `模型调用失败（角色 FLASH，provider=litellm）：ValueError: … → P0 验收请用 --provider mock，或让 Chief 配好 key` | — |
| **marker 字面量有两版**：D1-7 用 `[KFPA:GEN_TICKETS]`，R-3 用 `KFPA_TASK=qa_tickets`；磐石用别名兼容了两者，但注释写「过渡期…应删除」 | `llm.py:43-47`；台架探针实测 `d1_marker` 口径可跑 | 建议（Chief 定规范值） |

### 4.2 已裁定项（台架已跟随）

1. **原 A2/A3 冲突 → R-10**：维持 A2 单命令（默认剧本不含 HIGH），A3 走 `--inject high_ticket`。
   台架 A3 已从「`store.add_ticket` 造票」改为**纯 CLI 注入**，并加验「`tickets list --impact HIGH` 能看到该票」。
2. **原契约版本漂移 → R-12**：main 已 1.1，报告 0.1 里「仍 1.0 / D1 不存在」的结论已过时，0.2 起作废。
3. **原 batch-close 格式缺口 → D1-8/R-9**：映射格式已定义，台架 A7 按映射断言。

### 4.3 仍然开放

1. **【重要 · A8 缺图输入契约】** D1-12 定义了「用 LangGraph 编排」，但没定义 `build_graph()` 的
   签名与 `invoke` 的 state 结构；而 `tests/**` 归镜鉴，A8 的验收命令只能靠约定猜。本分支已交付
   `tests/test_graph_smoke.py`（对 `build_graph(*deps)` + `invoke({round_no, design_text, design_ref})`
   自适应尝试）；**建议 D1 补一行 state 契约**，否则图一改签名 A8 就要返工。
2. **【建议 · 验收表命令未同步 R-2】** `p0-plan §4` 的示例命令仍写 `python -m kfpa …`，
   而 R-2 要求统一 `PYTHONPATH=src`；A5 行也仍是那条会误杀 `llm.py` 的裸 grep（R-11 已给出两条新命令，表未同步）。
3. **【自查纠正】** 台架 A5.2 首轮把「无 marker 调用 `MockLLM().complete()` 抛 `MockScriptError`」误判为缺陷；
   复核 D1-7「未知 marker 必须抛错、禁止静默返回空串」后确认是**探针口径错**，已改为带 marker 调用并拆成 R-11.1/R-11.2/A5.3。

---

## 5. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 0.1 | 2026-10-16 | 首版：A1–A7 台架 + 契约测试 + 台架自检（PASS 1 / FAIL 8） | 镜鉴 |
| 0.2 | 2026-10-16 | 对齐 D1/D2：升 A1–A9，A4 走 `--inject`、退出码表、D1-9 六表列抽查、D1-6 tag、D1-8 事务、A8 图冒烟、A9 异常包装（PASS 1 / FAIL 9） | 镜鉴 |
| 0.3 | 2026-10-16 | 对齐 R-8/R-9/R-10/R-11：A5 三拆为 A5.1/A5.2/A5.3、A3 改纯 CLI `--inject high_ticket`、新增 A7.2；补 PR #3/#4 集成实测结论（2 阻塞 / 1 重要 / 1 建议） | 镜鉴 |

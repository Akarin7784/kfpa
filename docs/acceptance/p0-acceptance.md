# KFPA P0 独立验收报告

> 负责人：**镜鉴**（分支 `p0/verify`，工作树 `workspace\kfpa-jingjian`）
> 依据（效力从高到低）：`docs/dev/p0-rulings.md`（D2）→ `docs/dev/p0-design-addendum.md`（D1，契约 1.1）
> → `docs/dev/p0-plan.md` §4 验收标准 A1–A9。本报告只做**独立验证**，不改任何人写的实现。
> 结论必须在「命令 + 真实输出片段 + 结论」三件套齐备的前提下给出，禁止只写「已通过」。

---

## 0. 当前状态

| 项 | 值 |
|---|---|
| 验收台架 | `scripts/verify_p0.py`（A1–A10 共 13 条断言）＋ `scripts/selfcheck_core.py`（磐石自检面，96 项，第二意见） |
| 契约版本 | main `CONTRACT_VERSION 1.1`；裁决 D2 已到 **v1.6**（R-1 ~ R-19） |
| 最近一次全量运行 | 2026-10-16 **clean env**（不设 `PYTHONIOENCODING`/`PYTHONUTF8`、stdout 重定向）@ `main@51b15b3` + 本分支：`verify_p0.py` → **13 条断言 PASS 13 / FAIL 0**、`rc=0`；`selfcheck_core.py` → **96 PASS / 0 FAIL**、`rc=0`；`pytest tests -q` → **35 passed**、`rc=0` |
| 判定 | **P0 收口通过**（A1–A10 全绿，含 R-18/R-19 的编码健壮性） |
| 上一轮（同一批分支，合并前） | `main@0195ed7` + `core@b6c6fb0` + `ui@7892ac3` + `pipeline@cad3ea2` → PASS 10 / FAIL 3；三条红同一根因 R-18，已由 `p0/core@b390596`（Chief 代补）闭合 |

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

### 4.3 全链集成预演（R-16：只在本地临时分支，未推 main）

```
git checkout -B verify/all origin/main            # d46b41d
git merge origin/p0/core      # 0a5d25b
git merge origin/p0/ui        # 7892ac3
git merge origin/p0/pipeline  # d2a3833
git merge p0/verify           # 台架
PYTHONPATH=src python scripts/verify_p0.py       # 12 条断言 PASS 12 / FAIL 0 / SKIP 0
PYTHONPATH=src python -m pytest tests -q         # 32 passed
```

上一轮点名的问题在各自新 head 上均已消除：R-8 `Store.list_tickets(round_no, status, impact)` 已就位
（`tickets list` 从 `rc=5` 恢复 `rc=0`）；R-10 `--inject` 值域含 `high_ticket` 且 `build_llm(high_impact=True)` 可用；
R-1「设计文件不存在 → 4」已对齐；R-17 两条 marker（规范 `KFPA_TASK=` 与永久别名 `[KFPA:GEN_TICKETS]`）都可跑且输出一致。
A8 的图冒烟按 R-14 冻结契约（关键字参数签名 / `GRAPH_NODES` / `invoke` 三键 / `gate.state=="pass"`）实跑通过。

### 4.4 台架自身的误判（自查纠正，均已修）

| 误判 | 真相 | 修法 |
|---|---|---|
| A5.2 把「无 marker 调用 MockLLM 抛 `MockScriptError`」判缺陷 | D1-7 要求必须抛错、禁止静默返回空串 | 探针改为带 marker 调用；拆成 A5.1/A5.2/A5.3 |
| A4 查 `GateReport.error_category` | 该字段在契约 1.1 里根本不存在；D1-4 的 category 体现在 `reason_key` | 改为按 D1-4 公式 `sha1(category\|target)[:12]` 复算 |
| A3 用 `str(t.status).endswith("closed")` 判票状态 | `str(TicketStatus.CLOSED)` == `"TicketStatus.CLOSED"`，永远判否 | 统一走 `.value`（`status_value()` helper） |
| A3 断言放行后 `rounds.state == released` | `RELEASED` 是放行时点的瞬态，整轮跑完是 `snapshotted` | 改为断言拦停时 `tickets_open`、放行后不在 `tickets_open/blocked` |

### 4.5 R-18 编码健壮性（A10）—— 本轮唯一阻塞，也是「假绿」实证

Chief 在 PR #6 指出：我前几轮报的 A1/A7 `rc=0` 可能是**环境相关的假绿**。核对后确认成立 ——
台架的 `Ctx.env` 给所有子进程设了 `PYTHONIOENCODING=utf-8`，把这道题吞掉了。台架已按 R-18.4 改为
`clean_env=True`（不传 `PYTHONIOENCODING` / `PYTHONUTF8`）复跑 A1/A7，并新增 **A10**。补跑后缺陷现形：

```
python -c "import sys; print(sys.stdout.encoding, sys.stdout.isatty())"  →  cp1252 False
src/kfpa/cli.py  grep 'reconfigure|PYTHONIOENCODING'                     →  0 命中

main@0195ed7 + core@b6c6fb0 + ui@7892ac3 + pipeline@cad3ea2（clean-env，capture_output）
  --help                        → rc=1  UnicodeEncodeError   （D1-5 期望 0）
  init                          → rc=0                        ✅
  tickets list                  → rc=1  UnicodeEncodeError   （D1-5 期望 0）
  round --dry-run --json        → rc=1  UnicodeEncodeError   （D1-5 期望 0/10/11/3）
```

台架侧 A1、A7、A10 三条因此全红（同一个根因）。修法见 R-18.1：`cli.main()` 最前把
`sys.stdout/stderr` `reconfigure(encoding="utf-8", errors="replace")`。
`pipeline.py`（长流）已自带第二层降级（阶段日志 `except UnicodeEncodeError`），但救不了 `--help` 与 `tickets`。

### 4.6 仍然开放

1. **【阻塞】** 上条的 R-18 主兜底未落地（归磐石）。
2. **【建议 · 验收表命令未同步 R-2/R-11】** `p0-plan §4` 的示例命令仍写 `python -m kfpa …`
   （R-2 要求 `PYTHONPATH=src`），A5 行仍是那条会误杀 `llm.py` 的裸 grep（R-11 已给出两条新命令）。

---

## 5. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 0.1 | 2026-10-16 | 首版：A1–A7 台架 + 契约测试 + 台架自检（PASS 1 / FAIL 8） | 镜鉴 |
| 0.2 | 2026-10-16 | 对齐 D1/D2：升 A1–A9，A4 走 `--inject`、退出码表、D1-9 六表列抽查、D1-6 tag、D1-8 事务、A8 图冒烟、A9 异常包装（PASS 1 / FAIL 9） | 镜鉴 |
| 0.3 | 2026-10-16 | 对齐 R-8/R-9/R-10/R-11：A5 三拆、A3 改纯 CLI `--inject high_ticket`、新增 A7.2；PR #3/#4 集成实测 2 阻塞 / 1 重要 / 1 建议 | 镜鉴 |
| 0.4 | 2026-10-16 | 对齐 R-13/R-14/R-15/R-17；修台架四处误判；全链集成预演 **12/12 通过**（core 0a5d25b / ui 7892ac3 / pipeline d2a3833 / main d46b41d） | 镜鉴 |
| 0.3 | 2026-10-16 | 对齐 R-8/R-9/R-10/R-11：A5 三拆为 A5.1/A5.2/A5.3、A3 改纯 CLI `--inject high_ticket`、新增 A7.2；补 PR #3/#4 集成实测结论（2 阻塞 / 1 重要 / 1 建议） | 镜鉴 |
| 0.5 | 2026-10-16 | 新增 A10（R-18）与 `clean_env` 复跑 A1/A7；六表改七表（D1-9 含 meta）；A9 落到 `tests/test_llm_adapter.py`；集成预演 13 条 PASS 10 / **FAIL 3（R-18 编码兜底未落地，已到 PR #3）** | 镜鉴 |
| 0.6 | 2026-10-16 | 按 R-19 给 `verify_p0.py` / `selfcheck_core.py`补 stdio 兜底（独立脚本不过 `cli.main()`）；**clean env 全量收口：13/13 + selfcheck 96/0 + pytest 35 passed，rc 全 0** | 镜鉴 |

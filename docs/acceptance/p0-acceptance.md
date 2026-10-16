# KFPA P0 独立验收报告

> 负责人：**镜鉴**（分支 `p0/verify`，工作树 `workspace\kfpa-jingjian`）
> 依据（效力从高到低）：`docs/dev/p0-rulings.md`（D2）→ `docs/dev/p0-design-addendum.md`（D1，契约 1.1）
> → `docs/dev/p0-plan.md` §4 验收标准 A1–A9。本报告只做**独立验证**，不改任何人写的实现。
> 结论必须在「命令 + 真实输出片段 + 结论」三件套齐备的前提下给出，禁止只写「已通过」。

---

## 0. 当前状态

| 项 | 值 |
|---|---|
| 验收台架 | `scripts/verify_p0.py`（本分支交付，A1–A9 共 10 条断言） |
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
| A5 | `src/` 内模型字符串铁律（仅 `src/kfpa/llm.py` 白名单） | 全员 | **PASS**（命中 0 处） |
| A5.2 | 4 角色、MockLLM 确定性（D1-7）、非法 role 拒绝、`high_impact=True` 开关（R-3） | 磐石 | **FAIL**（缺 `llm.py`/`config.py`） |
| A6 | PASS 轮落 `snapshots` + tag `kfpa-r{n}`（D1-6）+ `history` 列出 | 磐石 | **FAIL**（缺 `store.py`/`cli.py`） |
| A7 | 台账列全（D1-8 七列）/过滤/`close` 落库/`batch-close` 映射 + 未知票号**整体不写**(退出码 2) | 界面 | **FAIL**（缺 `ui/cli.py`） |
| A8 | LangGraph 图冒烟（`build_graph` 可编译可跑，D1-12）＋纯函数核心仍有保险丝 | 长流 | **FAIL**（`build_graph` 未交付，冒烟 skip → 判 FAIL） |
| A9 | LiteLLM 适配器把底层异常统一包成中文 `RuntimeError` 且提示 `--provider mock`（R-7） | 磐石 | **FAIL**（缺 `llm.py`） |

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
| 真仓库跑 A5 | PASS | 通过 |

`tests/test_contract.py` 对契约 1.1 做独立复算（枚举、票面五要素、六段判定、`package_hash`
确定性、`dumps` 稳定性、v1.0/v1.1 字段门控）。

实测：`PYTHONPATH=src python -m pytest tests -q` → **30 passed, 2 skipped**（2 skip = A8 冒烟在
`build_graph` 交付前 skip，台架已把它判 FAIL）。

---

## 4. 独立复核发现

1. **【已裁定 · 原 A2/A3 冲突】** 硬规则与「一条 round 跑完整轮」的单命令粒度冲突，现由
   D1-1 硬规则（退出码 3）+ R-3（默认剧本只产 MEDIUM/LOW、`high_impact=True` 另造）解决。
   台架据此改为：A2 走默认剧本，A3 用 `store.add_ticket(impact=HIGH)` 独立造票。
2. **【已裁定 · 原契约版本漂移】** main 已升 1.1，`p0-design-addendum.md` 与 `p0-rulings.md` 均已落 main；
   台架的版本门控保留，用于点出未 rebase 的分支。
3. **【已裁定 · 原 batch-close 格式缺口】** D1-8 已定义 `票号 -> 结论` 映射与未知票号整体不写（退出码 2）。
4. **【重要 · A5 的示例命令仍不严谨】** `p0-plan §4/A5` 给的
   `grep -riE "deepseek|gpt-|claude|qwen|bge" src --include=*.py` 会把 `llm.py` 适配器自身判违规，
   与铁律「仅 llm.py 适配器与 config 白名单」冲突。台架按铁律原文实现为「命中必须全落在
   `src/kfpa/llm.py`」，并在报告里同时列出越界项与白名单项。建议把白名单写进验收表。
5. **【重要 · A8 缺图输入契约】** D1-12 定义了「用 LangGraph 编排」，但没定义 `build_graph()` 的
   签名与 `invoke` 的 state 结构；而 `tests/**` 按分工归镜鉴，A8 的验收命令
   `python -m pytest tests/test_graph_smoke.py` 因此只能靠约定猜。本分支已交付
   `tests/test_graph_smoke.py`，对 `build_graph(*deps)` + `invoke({round_no, design_text, design_ref})`
   做了自适应尝试；**建议 D1 补一行 state 契约**，否则图一改签名 A8 就要返工。
6. **【建议 · 验收表命令未同步 R-2】** `p0-plan §4` 的示例命令仍写 `python -m kfpa …`，
   而 R-2 要求统一 `PYTHONPATH=src`。建议同步，避免新人在没有 editable 安装的 clone 里跑出假红。

---

## 5. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 0.1 | 2026-10-16 | 首版：A1–A7 台架 + 契约测试 + 台架自检（PASS 1 / FAIL 8） | 镜鉴 |
| 0.2 | 2026-10-16 | 对齐 D1/D2：升 A1–A9，A4 走 `--inject`、退出码表、D1-9 六表列抽查、D1-6 tag、D1-8 事务、A8 图冒烟、A9 异常包装（PASS 1 / FAIL 9） | 镜鉴 |

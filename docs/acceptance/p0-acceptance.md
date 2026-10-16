# KFPA P0 独立验收报告

> 负责人：**镜鉴**（分支 `p0/verify`，工作树 `workspace\kfpa-jingjian`）
> 依据：`docs/dev/p0-plan.md` §4 验收标准 A1–A7（本报告只做**独立验证**，不改任何人写的实现）
> 结论必须在「命令 + 真实输出片段 + 结论」三件套齐备的前提下给出，禁止只写「已通过」。

---

## 0. 当前状态

| 项 | 值 |
|---|---|
| 验收台架 | `scripts/verify_p0.py`（本分支交付） |
| 契约版本（main @ 本轮） | `1.1` 未落 main，main 仍是 `1.0`；台架版本自适应 |
| 最近一次全量运行 | 2026-10-16，**9 条断言 → PASS 1 / FAIL 8 / SKIP 0** |
| 那一 条 PASS | `A5.1` 模型字符串铁律（main 基线本来就干净，见 §3） |
| 判定 | **P0 未通过**：`p0/core`、`p0/pipeline`、`p0/ui` 三个模块尚未交付，FAIL 全部指名缺失文件与责任模块 |

台架**不给未交付模块发通过**：契约文件缺失一律报 FAIL 并列出「缺哪个文件、归谁写」，
不使用 SKIP 掩盖（唯一允许 SKIP 的情形是契约版本里还没有该字段，见 §2）。

---

## 1. 怎么跑（离线，无网络、无 API key）

```bash
python scripts/verify_p0.py                  # 全量，退出码 0=无 FAIL / 1=有 FAIL / 2=台架用法错
python scripts/verify_p0.py --only A2,A5.1   # 只跑指定断言（支持前缀）
python scripts/verify_p0.py --json out.json  # 落机器可读证据，供 PR 评论引用
python scripts/verify_p0.py --list           # 列出全部断言与责任模块
python -m pytest tests -q                    # 契约测试 + 台架自检（30 项）
```

断言之间独立：每条各自建临时 SQLite 与临时 workdir，不依赖执行顺序。
除 A2 的 `--dry-run` 外不依赖网络、API key、`litellm` / `aider` / `streamlit` 安装。

---

## 2. A1–A7 逐条结论

「台架 id」是 `scripts/verify_p0.py` 里的断言编号；`A5` 拆成 `A5.1`（静态扫描）与 `A5.2`（适配器行为），
`A7.2` 是附加断言（契约 §3.7 有 `batch-close`，但 A7 表未列）。

| 台架 id | 验收项 | 责任模块 | 当前结论 |
|---|---|---|---|
| A1 | CLI 可用：`python -m kfpa --help` 退出码 0，含 `init/round/tickets/history` | 磐石 + 界面 | **FAIL**（缺 `src/kfpa/cli.py`、`__main__.py`、`ui/cli.py`） |
| A2 | 离线端到端 dry-run：票五要素齐、任务包六段齐、五表落库 | 磐石 + 长流 | **FAIL**（缺 `store.py`/`pipeline.py`/`examples/design-r1.md`） |
| A3 | 未关闭高影响票不得出任务包（CLI 退出码非零 + 中文；关票后放行） | 长流 | **FAIL**（缺 `pipeline.py`/`store.py`） |
| A4 | 三态闭环 + 回灌区 + 同类失败 ≥2 次 `BLOCKED`/`escalated` | 长流 | **FAIL**（缺 `gates.py`） |
| A5.1 | `src/` 内模型字符串铁律（仅 `src/kfpa/llm.py` 白名单） | 全员 | **PASS**（命中 0 处） |
| A5.2 | `litellm` 惰性导入、`MockLLM` 确定性、非法 role 拒绝、4 角色齐 | 磐石 | **FAIL**（缺 `llm.py`/`config.py`） |
| A6 | `snapshots` 有行且 `kfpa history` 列出 `package_hash` | 磐石 | **FAIL**（缺 `store.py`/`cli.py`） |
| A7 | `tickets list` 过滤可用、`tickets close` 落库 | 界面 | **FAIL**（缺 `ui/cli.py`） |
| A7.2 | `tickets batch-close` 攒批确认（A7 表未列） | 界面 | **FAIL**（未交付；另见 §4 第 4 条格式缺口） |

模块交付后本表逐行回填「命令 + 输出片段」。回填前的结论一律以上面这次全量运行为准。

### 版本自适应（不掩盖、不误杀）

| 契约版本 | 台架行为 |
|---|---|
| `1.0`（main 现状） | `ExecResult` 无 `error_category`/`failed_criteria`/`results`，`GateReport` 无 `reason_key` → A4 中依赖这些字段的子断言报 **SKIP** 并注明「等 v1.1 落 main 后启用」 |
| `1.1`（Chief 工作树，未提交） | 上述子断言全部启用；`tests/test_contract.py` 同步按版本断言默认值 |
| 其它 | A1 直接 **FAIL**：「契约版本 X 未同步到验收台架」——升版必须同步台架与 `SUPPORTED_CONTRACT_VERSIONS` |

---

## 3. 台架自检（先证伪验收工具本身）

`tests/test_verifier_selfcheck.py` 用**故意残缺/故意投毒**的桩仓库跑台架，防止出现「一张会变绿的纸」：

| 自检 | 期望 | 结果 |
|---|---|---|
| 桩仓库只有 `src/kfpa/__init__.py`，跑 A1 | FAIL，且诊断指名 `src/kfpa/cli.py` + 责任模块 | 通过 |
| 桩仓库 `src/kfpa/oops.py` 里放 `deepseek/...`，跑 A5.1 | FAIL，且命中文件被点出 | 通过 |
| 桩仓库 `src/kfpa/llm.py` 里放 `deepseek/...`，跑 A5.1 | PASS（白名单不误杀） | 通过 |
| 桩仓库 `CONTRACT_VERSION="9.9"`，跑 A1 | 诊断里出现 `9.9` | 通过 |
| `--only NOPE` | 退出码 2（用法错误，而非静默通过） | 通过 |
| 真仓库跑 A5.1 | PASS（main 基线无越界模型串） | 通过 |

`tests/test_contract.py` 另外对冻结契约做独立复算（枚举取值、票面五要素、六段判定、
`package_hash` 确定性、`dumps` 稳定性、版本门控字段），**30 项全绿**：
`python -m pytest tests -q  →  30 passed in 1.78s`。

---

## 4. 独立复核发现

1. **【阻塞·A2/A3 语义冲突】** design.md §4 的硬规则（未关闭高影响票不得出任务包）与 A2
   （一条 `kfpa round --dry-run` 命令跑完整轮）在单条命令粒度上互斥：只要 `MockLLM` 剧本产出的
   質問票里含 `HIGH` 且未关闭，A2 永远不可能绿。两条出路：(a) happy-path 剧本只产 `MEDIUM`/`LOW`，
   HIGH 票另做注入用例（A3 已经这么做）；(b) A2 的验收命令改成两段式「round → 人工关票 → round」。
   需要 Chief 在契约/验收表里定口径。
2. **【重要·A5 的原命令不严谨】** p0-plan §4/A5 给的是
   `grep -riE "deepseek|gpt-|claude|qwen|bge" src --include=*.py`，它会把 `llm.py` 适配器自身
   也算违规（而铁律明文允许适配器内部出现模型串）。台架因此实现为「命中必须全部落在
   `src/kfpa/llm.py`」，并同时打印越界项与白名单项。建议把该命令与白名单写进验收表。
3. **【重要·契约版本漂移】** main 上 `CONTRACT_VERSION = "1.0"`，而
   `p0-design-addendum.md`（v1.1 引用的设计补充件）在 main 上不存在。台架已做版本自适应，
   但**A4 的两条关键子断言（`results` 断言映射、错误分类）在 v1.1 落 main 前无法启用**，
   P0 不能据此算全过。
4. **【建议·`batch-close` 文件格式缺规格】** §3.7 只写了 `--file <answers.yaml|json>`，
   没有定义结构。台架按「`{票号: 结论}` 映射」与「`[{ticket_id, answer}]` 列表」两种常见格式
   探测，都失败时只给建议、不计阻塞。建议契约补一行格式定义，否则 A7.2 无法验收。

---

## 5. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 0.1 | 2026-10-16 | 首版：验收台架 + 契约测试 + 台架自检；首次全量为 PASS 1 / FAIL 8 | 镜鉴 |

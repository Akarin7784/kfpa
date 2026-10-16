# KFPA P3 分工与接口契约（D5 · 冻结 v1.0）

> 定位：按 `docs/design.md` §8 的 **P3 快车道 + 沉淀闸门全量落地**，兑现 design.md §5.5（知识沉淀闸门）
> 与 §5.6（维护轻量通道）里 P1/P2 只做了判据、没做落库的那一半，并首次接入检索（RAG）。
> 上位文档：`p0-plan.md`（施工图）· `p0-design-addendum.md`（D1）· `p0-rulings.md`（D2，R-1~R-32）·
> `p1-plan.md`（D3）· `p2-plan.md`（D4）。
> 本件为 **P3 唯一权威**；与 D1/D2/D3/D4 冲突处以本件为准（P3 范围内）。

## 0. 本轮范围

| 做 | 不做（留后） |
|---|---|
| **快车道承认落库**：`fastlane_requests` 表 + 攒批承认 + 审计留痕（兑现 R-23 的「P3 落库」） | 多模型成本优化 |
| **沉淀闸门全量**：置信度门槛 · 隔离区 · 复用转正 · 动态抽检 · 不合格丢弃 | Langfuse 仪表盘 |
| **负知识闭环**：失败记录 → 回避模式清单 → 下一轮任务包显式规避（R-28 轮次范围不变） | 全仓库覆盖率门槛（仍只算 R-32 纯逻辑白名单） |
| **检索接入（RAG）**：Dify 惰性适配器 + **本地回退检索**，命中进任务包 | 并行多功能流水线（非目标） |
| `verify_p3.py`（A23–A27）+ 台架交接 | PG 覆盖率的门槛化 |
| 清偿 #51 报的 `cli.py` 死分支（非阻塞项，见 §5） | |

**环境现实（2026-10-17 实测）**：本机**无任何模型 API key**、**Dify 未部署**。
因此 P3 的验收入口全部离线：快车道承认走 SQLite/本地 PG，检索走**本地回退实现**；
Dify 适配器**接线完成即算交付**，真调另开一次（需要用户给 key 与地址）。

## 1. 工作纪律（沿用 P0–P2，只列差异）

| 成员 | 分支 | 唯一写者范围 |
|---|---|---|
| 磐石 | `p3/store` | `src/kfpa/store/**`、`migrations/0003_fastlane.sql`、`src/kfpa/cli.py`（仅插件清单与子命令挂载）、`config/models.yaml` |
| 长流 | `p3/knowledge` | `src/kfpa/{fastlane,knowledge,rag,pipeline,gates}.py`、`src/kfpa/prompts/**`、`benchmarks/**` |
| 界面 | `p3/ui` | `src/kfpa/ui/**`、`reports/**` |
| 镜鉴 | `p3/verify` | `tests/**`、`scripts/verify_p3.py`、`docs/acceptance/p3-acceptance.md` |
| Chief | `p3/contract` | `src/kfpa/schemas.py`（冻结 1.4）、`docs/dev/p3-plan.md`、`diagrams/**`、`README.md` |

其余纪律同 D3 §1 / D4 §1：一人一工作树、交付走 PR、评审无阻塞即合 main、依赖由 Chief 统一装、
越界修改要报备（R-21）、**已合并的分支禁止续推**（R-29）、实现方桩落 `benchmarks/**`、由镜鉴改写进
`tests/**` 才能计入覆盖率（R-30）、合前 rebase、合后跑全量台架（P2 教训）。

**P2 踩过的坑，P3 开工前先钉死两条**：
1. **插件必须挂进 `cli.py` 的清单**：`fastlane` / `rag` 的 `register()` 写好了不等于 `kfpa fastlane` 存在
   （P2 的 `kfpa.eval` 就漏挂过，A21 的 CLI 路径直接跑不通）。磐石交付前必须跑
   `python -m kfpa fastlane --help` 与 `python -m kfpa rag --help` 确认子命令真的在。
2. **断言跟口径走，不跟当时实现走**（#48/#49 的教训）：本件 §3 冻结的口径改了，台架期望同批改。

## 2. 模块布局（P3 增量）

```
migrations/0003_fastlane.sql      # 磐石：fastlane_requests（PG DDL）＋ knowledge_samples
src/kfpa/fastlane.py              # 长流：判据纯函数 ＋ 攒批承认状态机 ＋ CLI register()
src/kfpa/knowledge.py             # 长流：闸门全量（门槛/隔离区/转正/抽检/丢弃）
src/kfpa/rag.py                   # 长流：Dify 惰性适配器 ＋ 本地回退检索 ＋ CLI register()
src/kfpa/prompts/fastlane.j2      # 长流：微型规格模板
src/kfpa/ui/fastlane_page.py      # 界面：快车道承认页（落库后是真承认，不再是预览）
src/kfpa/ui/knowledge_page.py     # 界面：隔离区/抽检页
reports/fastlane-*.md|html        # 界面：承认台账与抽检报告渲染产物
scripts/verify_p3.py              # 镜鉴：A23–A27
```

## 3. 冻结接口

### 3.1 数据契约 —— `schemas.py` 升 1.4（Chief 冻结）

```python
CONTRACT_VERSION = "1.4"

class FastlaneStatus(str, Enum):
    PENDING  = "pending"      # 已登记待承认
    APPROVED = "approved"     # 攒批承认通过（唯一可直跑的态）
    REJECTED = "rejected"     # 人工拒绝
    ESCALATED = "escalated"   # 触碰设计边界 → 升级回完整设计回合（见 A24）

@dataclass
class FastlaneRequest:
    request_id: str                 # fl-<round>-<seq>
    round_no: int
    summary: str                    # 微型规格一句话
    diff_files: list[str]           # 三来源优先级沿用 R-23（执行产物 → --files → no_diff_source）
    new_interface: bool             # 判据 1：是否新增/修改接口契约
    new_data_model: bool            # 判据 2：是否改数据模型
    touched_modules: list[str]      # 判据 3：波及模块（>1 → 不符）
    status: FastlaneStatus = FastlaneStatus.PENDING
    batch_id: str = ""              # 攒批承认批次（同批共享）
    decided_by: str = ""            # 承认人（CLI 传 --by，默认 $USER）
    decided_at: float = 0.0
    note: str = ""

@dataclass
class KnowledgeSample:
    """人工抽检留痕（design.md §5.5「置信度门槛 + 人工抽检」）。"""
    cid: str
    sampled_at: float
    sampled_by: str
    rate: float                     # 抽中时的动态抽样率（留痕，便于回看口径）
    passed: bool                    # 抽检结论：False → 该候选 dropped

# TaskPackage 增一个字段（进任务包，检索命中）
knowledge_hits: list[str] = field(default_factory=list)   # 形如 "K1#reused=2" 的稳定摘要串
```

`SCHEMA_VERSION` → `"4"`（新增 `P3_SCHEMA_VERSION = "4"`；`P0/P1/P2` 常量不动）——
**随 `0003_fastlane.sql` 一同落地**，落地那一批必须同时更新 `selfcheck_core.py` 的合法集与
`verify_p0/p1/p2` 的守卫（R-20/R-21 同批同步）。
契约 1.4 已随本件冻结（`schemas.py`）：新增 `FastlaneStatus` / `FastlaneRequest` / `KnowledgeSample`，
`TaskPackage` 增 `knowledge_hits`；`verify_p0/p1/p2` 与 `tests/test_contract.py` 的支持集同批收 1.4。

### 3.2 快车道落库与攒批承认（长流判据 + 磐石存储）

- **判据（design §5.6，纯函数，图只做编排）**：`judge(request) -> bool`
  —— 三项全真才算快车道：不新增/不修改接口契约 · 不改数据模型 · 波及面 ≤ 1 个模块。
  任一为假 → `ESCALATED`，并在 reasons 里指名是哪一条（不许只给个布尔）。
- **命令**（长流实现 `register()`，磐石挂清单）：
  `kfpa fastlane plan --round N [--files a,b]` · `kfpa fastlane list [--status pending]` ·
  `kfpa fastlane ack <request_id> --by <name>` · `kfpa fastlane batch-ack --file acks.yaml --by <name>` ·
  `kfpa fastlane show <request_id>`
- **该组的唯一 owner 是 `kfpa/fastlane.py`（R-34）**：P1 的 `check` 一并由它注册（内部转
  `kfpa.impact.analyze`），`kfpa.impact` 不再注册 `fastlane`；`cli.py` 插件清单里
  `("kfpa.impact", ".impact")` → `("kfpa.fastlane", ".fastlane")`。
- **`acks.yaml` 文件 schema（R-34 冻结）**：

  ```yaml
  acks:
    - request_id: fl-1-1
      note: 可选的承认备注
  ```

  缺 `acks` 键或元素缺 `request_id` → **rc=2 + 中文**（用法错误），不许静默跳过、不许凭空补行。
- **幂等与留痕**：同一 `request_id` 二次承认不重复写行、不覆盖首次 `decided_by/decided_at`，返回原记录；
  攒批承认同批共享 `batch_id`；`ESCALATED`/`REJECTED` 的请求**不许**被 `ack` 翻成 `APPROVED`（要翻必须新开请求）。
- **存什么**：`fastlane_requests` 表（SQLite 与 PG 同签名），`kfpa fastlane list --json` 输出稳定列。

### 3.3 沉淀闸门全量（长流）

1. **置信度门槛维持 `CONFIDENCE_THRESHOLD = 0.80`**（D3 §3.5 / A15 原文；R-33 裁定）——
   `candidate.confidence < 0.80` → 直接 `DROPPED`，**不进隔离区**（design §5.5「不合格沉淀直接丢弃」）。
   *（D5 v1.0 曾误写成 0.6，R-33 已改回；A15 口径不回退、期望不改。）*
2. **隔离区**：过门槛的先落 `QUARANTINE`；只有 `can_promote()`（被 **≥1 个后续轮次**成功复用）才转正。
   **后续**判定：`reused_by` 里的轮次必须 `> candidate.round_no`，同轮复用不算（防自证）。
3. **动态抽检**：`sampling_rate(successful_reuse_count)` 沿用 D3-3.5 的分档；被抽中的候选进抽检队列并写
   `KnowledgeSample` 留痕；抽检 `passed=False` → `DROPPED` **且**把内容写进回避模式清单（负知识）。
4. **报告**：`kfpa knowledge report --out reports/knowledge-report.json`（稳定键：各状态计数、抽检率、
   被丢弃清单、转正清单）。

**冻结的出口名（R-33，台架按名硬探，探不到 = FAIL）**：

```python
kfpa.knowledge.admit(store, candidate) -> KnowledgeCandidate     # 闸门唯一入口（P1 已有，语义不变）
kfpa.knowledge.record_sample(store, cid, passed, *, sampled_by="", rate=0.0) -> KnowledgeSample
# 既有出口不动：sampling_rate() / promotable() / mark_reused() / make_candidate() /
#               is_sampled() / review_sample()
# CLI：kfpa knowledge sample | verdict <cid> --pass/--fail | report
```

**快车道出口名（R-33，同批冻结）**：

```python
kfpa.fastlane.judge(request) -> bool                    # 三判据纯函数（等价 request.judged_fastlane()）
kfpa.fastlane.plan(store, ...)  -> FastlaneRequest      # 登记 pending（不满足判据 → ESCALATED）
kfpa.fastlane.ack(store, request_id, by) -> FastlaneRequest
kfpa.fastlane.batch_ack(store, request_ids, by) -> list[FastlaneRequest]
# CLI：kfpa fastlane plan | list | ack | batch-ack | show
```

### 3.4 负知识闭环（长流，与 R-28 同口径）

失败记录 → 回避模式清单（`FailureRecord.avoid_pattern`）→ **下一轮**任务包 `feedback` 显式规避。
回灌范围**只取「本轮 + 上一轮」**（R-28 原文，禁止全库无轮次过滤——否则同名轮次 `package_hash` 两库不一致）。
A26 直接断 SQLite/PG 两侧 `package_hash` 一致。

### 3.5 检索接入 RAG（长流 + Chief 冻结的契约）

- **本地回退（验收路径，离线确定）**：对 `knowledge` 表（`PROMOTED` 优先）+ `docs/**` 做朴素关键词召回，
  返回**稳定排序**的命中列表（同输入同输出），命中摘要进 `TaskPackage.knowledge_hits`。
- **Dify 适配器（接线即交付）**：`DIFY_API_URL` / `DIFY_API_KEY` 环境变量；未配置或未装依赖 →
  **rc=4 + 中文**（不是 5，也不是裸崩）；真实 HTTP 调用只在配了凭据时发生。
- **不许漏密钥**：任何日志、异常消息、DB 落库内容里禁止出现 key 明文（沿用 DSN 打码口径，A27 断）。
- **降级可见**：本地回退命中时，任务包/报告要能看出「这是回退结果」（`provider="local"`），
  不许把回退结果伪装成 Dify 结果。

### 3.6 报告与人机界面 —— `ui/**` / `reports/**`（界面）

- 快车道承认页：从「预览：P3 才接攒批承认入库」改成**真承认**（写库 + 显示 `batch_id/decided_by/decided_at`），
  保留一次性确认的防误点（每批必须显式勾选条数一致才放行）。
- 知识库页：隔离区列表 + 抽检队列 + 转正/丢弃清单，渲染函数必须可 **headless** 调用（不列入验收）。
- 报告产物：`reports/fastlane-<批次>.md`、`reports/knowledge-report.html`（**单文件、无 CDN、无外链**，可离线打印）。

## 4. 验收标准 A23–A27（镜鉴独立验证，clean env）

> 沿用 R-19/R-25/R-27：台架自带 stdio 兜底、剥掉 `KFPA_DB_URL`、测 rc 先落文件、探针做驱动归一；
> 新增 **R-32 口径**：覆盖率仍只算纯逻辑白名单（`store/postgres.py` 只作报告项）。
> 台架内嵌 **A0 = P0/P1/P2 不回归守卫**（13/13 + 8/8 + 6/6）。

| 编号 | 断言 | 关键命令 |
|---|---|---|
| A23 | **快车道落库与攒批承认**：`plan` 登记 → `ack`/`batch-ack` 落 `fastlane_requests`；重复承认幂等；`batch_id` 同批共享；`ESCALATED`/`REJECTED` 不可被翻成 `APPROVED`；`schema_version="4"` | `python -m kfpa fastlane plan …` / `… list --json` |
| A24 | **快车道判据与升级**：三判据全真才 `PENDING→APPROVED`；任一为假 → `ESCALATED` 且 reasons 指名是哪条；`no_diff_source`（R-23 第三来源）显式可见 | `python -m kfpa fastlane plan --round N` |
| A25 | **沉淀闸门全量**：低于置信度门槛（**0.80**）直接 `DROPPED`（不进隔离区）；`QUARANTINE` 只被**后续轮次**复用才转正（同轮不算）；抽检 `passed=False` → `DROPPED` + 写回避模式；抽检留痕可查。**入口名按 §3.3 冻结**（`admit` / `record_sample`），探不到 = FAIL | `python -m kfpa knowledge list/promote/sample/verdict/report` |
| A26 | **负知识回灌 + 双库一致**：失败记录进下一轮任务包 `feedback`；回灌只取本轮+上一轮；同轮次在 SQLite 与 PG 上 `package_hash` 一致。**PG 夹具复用 A12**：`KFPA_PG_PORT`（默认 55440）连不上 → 可见 SKIP；`KFPA_VERIFY_PG_DSN` 为显式覆盖；**不自建容器**（R-24） | `verify_p1.py --only A12` 扩到新表 + `round` 两轮对拍 |
| A27 | **RAG 检索**：本地回退可离线命中且**稳定排序**；未配 Dify 凭据 → rc=**4** + 中文；异常/日志/库里**无 key 明文**；回退结果标 `provider="local"` | `python -m kfpa rag search "…" --json` |
| A0 | **不回归**：`verify_p0` 13/13、`verify_p1` 8/8、`verify_p2` 6/6、`pytest` 全绿 | 三条台架 + `pytest -q` |

**A0 与 A23–A27 的地位不同（R-33）**：A0 **必须始终绿**；A23–A27 在实现落地前允许红，
但每条红必须指名到人与接口——**红的是派工单，不是 main 回归**。P3 收口 = A23–A27 全绿。

**A20 的 P3 增量（R-33 配套，收口时生效）**：新模块不能「门槛绿而自己没测」。
- `knowledge.py`、`fastlane.py` 两块**纯逻辑**加入 A20 门槛白名单（各 ≥80%），由 `verify_p3` 的覆盖率段断；
  `rag.py` 作**报告项**（Dify 分支在无凭据环境下不可达，与 `store/postgres.py` 同理，不算门槛）；
  PG 驱动的覆盖仍由 A12/A26 承担。
- 报告项要看得见：`rag.py` 与 `store/postgres.py` 的覆盖率随 A20b 一并打印。

验证要求同前：命令 + 真实输出片段 + 结论；断言独立；失败指名到模块与接口；SKIP 只用于环境不具备且必须在报告里可见。

## 5. 顺带清偿（非阻塞，归磐石）

`cli.py` 256-261 是**死分支**（#51 的镜鉴报的）：`GatesError` 继承 `RuntimeError`，
`except (OSError, ValueError)` 抓不到，那段「覆盖率报告不可用 → 退出码 4」的中文永远不会打印。
修法二选一：把 `GatesError` 加进 except，或删掉死提示；**无论选哪个都要补一条测试证明该中文会打印**
（或证明提示确实被移除），并把结论写进 PR 说明。

## 6. 依赖与环境

| 项 | 状态 |
|---|---|
| 模型 API key | **暂无** → 真实 LLM 调用不在 P3 验收范围（快车道直跑走 mock 执行器） |
| Dify 自托管 | 未部署 → 本地回退检索是验收路径；Dify 适配器接线完成即交付 |
| PostgreSQL（A12/A26 双库对拍） | 就绪（Docker 29.8.2，端口 55440 独占） |
| 覆盖率工具 | 已装（`coverage` / `pytest-cov`） |
| `streamlit` | 已装（界面页可 headless 冒烟，不列入验收） |

## 7. 合并门禁（与 P0–P2 同）

1. 每人分支 → PR → 评审无阻塞即合 `main`；
2. 每合一步，在 `main` 上跑 `verify_p0 + verify_p1 + verify_p2` 全量，**P0/P1/P2 不许回归**；
3. `verify_p3` 的 A23–A27 全绿 + `pytest` 全绿 + R-32 白名单覆盖率不下降（≥80%）；
4. 全部合完后由 Chief 跑干净环境终验、更新进度地图与 README，并写 `docs/acceptance/p3-acceptance.md`。

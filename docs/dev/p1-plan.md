# KFPA P1 分工与接口契约（D3 · 冻结 v1.0）

> 定位：按 `docs/design.md` §8 的 **P1 正经版**，把 P0 的最小闭环补成「可运营」的形态。
> 上位文档：`docs/dev/p0-plan.md`（P0 施工图）· `p0-design-addendum.md`（D1）· `p0-rulings.md`（D2，R-1~R-19）。
> 本件为 **P1 唯一权威**；与 D1/D2 冲突处以本件为准（P1 范围内）。

## 0. 本轮范围

| 做 | 不做（P2/P3） |
|---|---|
| PostgreSQL 双驱动（SQLite 保留，PG 走 Docker） | 评审位异构模型接入（P2） |
| token 账本（本地表必做，Langfuse 可选上报） | 快车道全量与沉淀闸门全量（P3 收口） |
| LangGraph `SqliteSaver` checkpoint 迁移（`thread_id=round_no`） | 并行多功能流水线（非目标） |
| 审查聚合折算规则（REVIEWER → 门禁，可量化） | 真 PR-Agent 云端集成（做适配器 + 折算） |
| 覆盖率门槛进入门禁（G7） | 全仓库 100% 覆盖 |
| 知識沉淀闸门（隔离区 + 转正规则） | 知识库中毒的自动检测算法 |
| 快车道影响分析判据（可计算） | 自动改需求/重规划 |
| Streamlit 質問票台账真跑（headless 验收） | 台账的多用户/权限体系 |

**环境实测（2026-10-16，Chief）**：Docker `29.8.2` daemon 运行中；`127.0.0.1:5432` 空闲；
pip 可达且 `psycopg-binary 3.3.6` 有 cp314 wheel；`streamlit`/`coverage`/`langfuse` 未装（由 Chief 统一装）。

## 1. 工作纪律（沿用 P0，只列差异）

1. **一人一工作树**（同 P0 表格，目录换成 `kfpa-p1-<成员>`），分支前缀 `p1/`：

   | 成员 | 分支 | 唯一写者范围 |
   |---|---|---|
   | 磐石 | `p1/store` | `src/kfpa/store/**`、`migrations/**`、`docker-compose.pg.yml`、`pyproject.toml` |
   | 长流 | `p1/pipeline` | `src/kfpa/{pipeline,gates,executor,ledger,impact,knowledge}.py`、`src/kfpa/prompts/**` |
   | 界面 | `p1/ui` | `src/kfpa/ui/**` |
   | 镜鉴 | `p1/verify` | `tests/**`、`scripts/**`、`docs/acceptance/**` |

2. `src/kfpa/schemas.py` 仍由 **Chief 冻结**；本轮升 `CONTRACT_VERSION = "1.2"`（见 §3.1）。
3. 交付走 PR；镜鉴独立验证后在 PR 评论给结论；Chief 复核 diff 后合并，顺序由 Chief 控。
4. 依赖由 Chief 统一装。**任何人不得 `pip install`、不得 `docker volume rm`、不得改别人的 compose 文件。**
5. PostgreSQL 相关命令统一走 `docker-compose.pg.yml`（磐石交付），端口 `55432:5432` 避免占用本机 5432。

## 2. 模块布局（P1 增量）

```
migrations/0001_init.sql          # 磐石：PG DDL（与 SQLite 同构）
docker-compose.pg.yml             # 磐石：PG 16 + 健康检查 + 固定端口 55432
src/kfpa/store/__init__.py        # 磐石：make_store() 工厂 + Store 协议
src/kfpa/store/sqlite.py          # 磐石：现 store.py 迁移（行为不变）
src/kfpa/store/postgres.py        # 磐石：psycopg3 驱动，同一套方法签名
src/kfpa/ledger.py                # 长流：token 账本（本地表 + Langfuse 可选上报）
src/kfpa/impact.py                # 长流：快车道影响分析（纯函数）
src/kfpa/knowledge.py             # 长流：沉淀闸门（隔离区/转正/丢弃）
src/kfpa/ui/streamlit_app.py      # 界面：真跑 + headless 冒烟
src/kfpa/ui/fastlane.py           # 界面：快车道攒批承认界面
scripts/verify_p1.py              # 镜鉴：A11–A17 台架（与 verify_p0.py 并存）
```

## 3. 冻结接口

### 3.1 数据契约 —— `schemas.py` 升 1.2（Chief 冻结）

新增三件（其余不动）：

```python
@dataclass
class LedgerEntry:
    round_no: int
    role: str            # ARCHITECT / REVIEWER / FLASH / EMBEDDING
    model_ref: str       # 从 config/models.yaml 的 model 字段取，不写死
    tokens_in: int
    tokens_out: int
    cost: float = 0.0    # 未配置单价时为 0.0，不许编造
    source: str = "mock" # mock | litellm | langfuse
    created_at: float = ...

@dataclass
class KnowledgeCandidate:
    cid: str
    round_no: int
    kind: str            # success | avoid（负知识）
    content: str
    confidence: float    # 0.0 ~ 1.0
    status: str          # quarantine | promoted | dropped
    reused_by: list[int] = ...   # 成功复用的轮次号
    created_at: float = ...

@dataclass
class CoverageReport:
    line_rate: float             # 0.0 ~ 1.0
    files: dict[str, float]      # 文件 -> 行覆盖率
    critical_paths_ok: bool      # A1–A10 台架是否全绿
    threshold_line: float = 0.80
    def passed(self) -> bool: ...
```

### 3.2 存储 —— `src/kfpa/store/**`（磐石）

```python
def make_store(dsn: str | None = None) -> Store:
    """dsn 为空 → SQLite（默认 kfpa.db）；postgresql://… → PG 驱动。环境变量 KFPA_DB_URL 兜底。"""

class Store(Protocol):                      # 现有方法全保留（P0 契约不破）
    # … P0 的 21 个方法不变 …
    # P1 新增
    def add_ledger_entry(self, entry: LedgerEntry) -> None: ...
    def list_ledger(self, round_no: int | None = None) -> list[LedgerEntry]: ...
    def add_candidate(self, c: KnowledgeCandidate) -> None: ...
    def get_candidate(self, cid: str) -> KnowledgeCandidate | None: ...
    def list_candidates(self, status: str | None = None) -> list[KnowledgeCandidate]: ...
    def promote_candidate(self, cid: str, reused_by_round: int) -> KnowledgeCandidate: ...
    def drop_candidate(self, cid: str, reason: str) -> None: ...
```

- 表：P0 七表 + `ledger` + `knowledge`（`schema_version = "2"`）。
- **双驱动行为必须一致**：同一断言集在 SQLite 与 PG 上都要绿（镜鉴做对拍，A12）。
- SQLite 侧保持 `:memory:` 可用；PG 侧 DSN 泄漏不得进日志（打印时打码）。

### 3.3 门禁 G7 与审查折算 —— `src/kfpa/gates.py`（长流）

- **G7 覆盖率门槛**：`CoverageReport.passed()` 为假 → `NEEDS_FIX`，`error_category="coverage"`，
  `failing_criteria` 填未达标文件。阈值 `line_rate >= 0.80` 且 `critical_paths_ok is True`。
- **G8 审查折算（可量化，禁止模型拍脑袋）**：

  | REVIEWER 结果 | 折算 |
  |---|---|
  | `severity=major` 且带 `criterion` 字段 | 加入 `failed_criteria` → `NEEDS_FIX` |
  | `severity=major` 无 `criterion` | 记 `reasons`，**不阻塞**（无法定位到断言的评审不进门禁） |
  | `severity=minor` / `info` | 只记 `reasons` |
  | 解析失败 / 非 JSON | 记 `reasons` + 日志，**不改三态**（R-5 不变） |

- 真 PR-Agent 只作为**可选适配器**（缺包/缺 key → 中文提示，降级为 REVIEWER 剧本）。

### 3.4 账本 —— `src/kfpa/ledger.py`（长流）

```python
class Ledger(Protocol):
    def record(self, entry: LedgerEntry) -> None: ...
def make_ledger(store, provider: str = "local") -> Ledger:
    """local：只落本地表（离线可验收）；langfuse：额外上报，失败只警告不崩。"""
def estimate_tokens(text: str) -> int:
    """确定性估算（字符数 // 3），mock 与无 usage 回包时使用；真实 usage 优先。"""
```

- 每次 `LLM.complete()` 必须产生一条 `LedgerEntry`（wrapper 实现，不散落在调用点）。
- CLI：`kfpa ledger [--round N] [--json]` 输出每轮每角色 token 与合计。

### 3.5 checkpoint 迁移与沉淀闸门 —— `src/kfpa/pipeline.py` / `knowledge.py`（长流）

- `build_graph(..., checkpointer=None)`：P1 默认 `SqliteSaver`（`langgraph-checkpoint-sqlite`），
  `thread_id = f"kfpa-r{round_no}"`；重跑同轮**复用 checkpoint**，不得重复开票（幂等靠 R-6 的票面保留 + checkpoint）。
- `knowledge.py` 规则（D1-13 第 3 条的落地）：

  | 阶段 | 规则 |
  |---|---|
  | 入隔离区 | `confidence >= 0.80` 且非空内容；低于阈值直接 `dropped` |
  | 转正 | 被 ≥1 个**成功轮次**（门禁 PASS）复用 → `promoted` |
  | 负知识 | `kind=avoid` 直接进任务包回灌区，但不进知识库正区 |
  | 抽检 | 抽样率 `max(0.05, 0.20 - 0.05 * 成功复用次数)`，封顶 0.20 / 下限 0.05 |

- CLI：`kfpa knowledge list [--status quarantine|promoted|dropped]`、`kfpa knowledge promote <cid> --round N`。

### 3.6 快车道影响分析 —— `src/kfpa/impact.py`（长流）

```python
def analyze(task_package: TaskPackage, diff_files: list[str]) -> ImpactReport:
    """纯函数。判据（design.md §5.6，全部可计算）：
       ①不新增/不修改接口契约 ②不改数据模型（migrations/** 与 schemas.py 未动）
       ③波及面 <= 1 个模块（按 diff_files 的顶层模块去重计数）
    三者全真 → fast_lane=True，否则 False + 触碰的边界清单。"""
```

- CLI：`kfpa fastlane check --round N`；`fast_lane=False` 时必须打印「升级回完整设计回合」。

### 3.7 Streamlit 台账 —— `src/kfpa/ui/**`（界面）

- `streamlit run src/kfpa/ui/streamlit_app.py -- --db kfpa.db` 真跑；新增快车道攒批承认页 `ui/fastlane.py`。
- 缺 `streamlit` → 中文提示 + rc=4（D1-5 不变）；**验收走 headless**：
  `python -c "from kfpa.ui.streamlit_app import main; …"` 直接调用渲染函数（不进浏览器）。
- 台账列、攒批事务规则不变（D1-8 / R-9）。

## 4. 验收标准 A11–A17（镜鉴独立验证，clean env）

> 沿用 R-19：台架自身必须做 stdio 兜底；测 rc 先落文件再读；`--json` 取最后一行解析。

| 编号 | 断言 | 命令（示例） |
|---|---|---|
| A11 | token 账本离线可用：mock 跑一轮后 `ledger` 表按角色有记录，`kfpa ledger --round N` 输出合计；无 Langfuse 也不崩 | `python -m kfpa ledger --round 1` |
| A12 | **双库对拍**：`docker compose -f docker-compose.pg.yml up -d` 后 `KFPA_DB_URL=postgresql://…:55432/kfpa` 跑 A2 同一命令，断言集与 SQLite 一致（含七表 + ledger + knowledge） | 两库各跑一遍 `verify_p1.py --only A2` |
| A13 | checkpoint：`SqliteSaver` 落盘且 `thread_id=kfpa-r{n}`；同轮重跑不重复开票、票面状态保留 | `python -m kfpa round … --round 1` 两次 |
| A14 | G7 覆盖率：`pytest --cov` 产出报告；低于 80% 行覆盖或关键路径不全 → `NEEDS_FIX` + `error_category=coverage` | `python -m pytest --cov=kfpa --cov-report=json` |
| A15 | 沉淀闸门：低置信候选直接 `dropped`；隔离区候选未经成功轮复用不得 `promoted`；`avoid` 进回灌不进正区 | `kfpa knowledge list` |
| A16 | 快车道判据：三判据全真 → `fast_lane=True`；动 `migrations/**`、`schemas.py` 或跨 2 模块 → `False` + 边界清单 | `kfpa fastlane check --round N` |
| A17 | Streamlit headless：渲染函数可调用并返回表格数据；缺 streamlit 时中文提示 + rc=4，不 ImportError 崩 | `python -c "from kfpa.ui.streamlit_app import main; …"` |

验证要求沿用 P0 §4：命令 + 真实输出片段 + 结论；断言独立；失败指名到模块与接口。

## 5. 环境与依赖（Chief 统一装）

| 依赖 | 用途 | 备注 |
|---|---|---|
| `psycopg[binary] >= 3.2` | PG 驱动 | 实测 `3.3.6` 有 cp314 wheel |
| `coverage` + `pytest-cov` | A14 覆盖率 | 纯 Python |
| `streamlit` | A17 台账 | 缺失时降级为 rc=4 提示 |
| `langfuse` | 账本上报（可选） | 装不上不影响 A11（本地表为准） |
| Docker 镜像 `postgres:16-alpine` | A12 | 首次 `docker compose up` 需联网拉取 |

## 6. 合并顺序与门禁

1. `p1/store`（磐石，含 compose 与迁移）→ 2. `p1/pipeline`（长流）→ 3. `p1/ui`（界面）→ 4. `p1/verify`（镜鉴）。
2. 每合一步：镜鉴在 main 上跑 `verify_p0.py` + `verify_p1.py` 全量；**P0 台架不允许回归**（13/13 必须保持）。
3. 冲突处理同 D2 R-16：预演只在本地临时分支，禁止推 main。

## 7. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 1.0 | 2026-10-16 | 首版冻结：P1 范围、四人分工与写者边界、契约 1.2、G7/G8 量化判据、A11–A17、依赖与环境实测 | Chief |

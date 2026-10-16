# KFPA P2 分工与接口契约（D4 · 冻结 v1.0）

> 定位：按 `docs/design.md` §8 的 **P2 评审位异构化**，消化「一个模型两挡位」的同源盲区债务，
> 并把 P1 留下的覆盖率债一并清掉。
> 上位文档：P0 施工图 `p0-plan.md` · 补充件 `p0-design-addendum.md`（D1）· 裁决 `p0-rulings.md`（D2，R-1~R-28）· P1 `p1-plan.md`（D3）。
> 本件为 **P2 唯一权威**；与 D1/D2/D3 冲突处以本件为准（P2 范围内）。

## 0. 本轮范围

| 做 | 不做（P3） |
|---|---|
| **按角色解析 provider**（REVIEWER 可独立指向异构模型） | 快车道承认落库 |
| 评审位 **fresh context** 真正落实（不共享 ARCHITECT 会话） | RAG（Dify）接入 |
| REVIEWER 接入主循环 + G8 折算留痕 | 沉淀闸门全量 |
| **同源盲区度量**：种子集 + `kfpa eval review` + 四项指标 | 多模型成本优化 |
| **覆盖率债清偿**：核心白名单行覆盖 ≥80% | 全仓库覆盖率门槛 |
| 度量报告（JSON + Markdown，可选 HTML） | Langfuse 仪表盘 |

**环境现实（2026-10-16 实测）**：本机**没有任何模型 API key**（`list_secrets` 为空），
因此 P2 的验收入口必须是**离线**的 `mock_hetero` provider；真实异构模型走 `litellm` 适配器，
**接线完成即算交付**，真调需要用户提供 key（另开一次）。

## 1. 工作纪律（沿用 P0/P1，只列差异）

| 成员 | 分支 | 唯一写者范围 |
|---|---|---|
| 磐石 | `p2/router` | `src/kfpa/{config,llm,cli}.py`、`src/kfpa/store/**`、`migrations/0002_reviews.sql`、`config/models.yaml` |
| 长流 | `p2/review` | `src/kfpa/{pipeline,gates,review,eval}.py`、`src/kfpa/prompts/review.j2`、`benchmarks/review/**` |
| 界面 | `p2/eval-ui` | `src/kfpa/ui/**`、`reports/**`（渲染产物） |
| 镜鉴 | `p2/verify` | `tests/**`、`scripts/**`、`docs/acceptance/**` |

其余纪律同 D3 §1：一人一工作树、`schemas.py` 由 Chief 冻结、交付走 PR、依赖由 Chief 统一装、
越界修改要报备（R-21）、集成预演只在本地（R-16）。

## 2. 模块布局（P2 增量）

```
migrations/0002_reviews.sql     # 磐石：reviews 表（PG DDL）
config/models.yaml              # 磐石：每角色可加 provider: 字段（REVIEWER 指异构）
src/kfpa/review.py              # 长流：评审调用与解析（fresh context + G8 折算输入）
src/kfpa/eval.py                # 长流：同源盲区度量（种子集 → 四项指标 → 报告）
benchmarks/review/seed.jsonl    # 长流：种子集（已知缺陷样本）
reports/review-eval.json|md     # 界面：度量报告渲染产物
src/kfpa/ui/eval_page.py        # 界面：报告页（可选，不列入验收）
```

## 3. 冻结接口

### 3.1 数据契约 —— `schemas.py` 升 1.3（Chief 冻结）

```python
class Severity(str, Enum):
    MAJOR = "major"; MINOR = "minor"; INFO = "info"

@dataclass
class ReviewFinding:
    severity: Severity
    location: str
    issue: str
    suggestion: str = ""
    criterion: str = ""          # 对应验收断言 cid；空 = 无法定位（G8 不阻塞）

@dataclass
class ReviewReport:
    round_no: int
    provider: str                # mock_hetero | litellm | …
    model_ref: str               # 从 config 取，不许写死
    context_id: str              # fresh context 标识（不得等于 ARCHITECT 的）
    parse_ok: bool               # 解析成功与否
    verdict: str                 # pass | fail | unknown
    findings: list[ReviewFinding] = ...
    created_at: float = ...

@dataclass
class ReviewMetrics:
    corpus: str                  # 种子集名
    total: int
    missed: int                  # 漏检
    false_alarms: int            # 误报
    agreement: float             # 与人工标注一致率 0~1
    baseline_missed: int = 0     # 同源 baseline 的漏检数（对比用）
    def miss_rate(self) -> float: ...
    def false_alarm_rate(self) -> float: ...
```

### 3.2 多 provider 路由 —— `config.py` / `llm.py` / `cli.py`（磐石）

```python
def resolve_provider(roles, role: str, override: str | None = None) -> str:
    """优先级：CLI 全局 --provider > 角色自己的 provider > 默认 mock。"""
def build_llm(roles, provider: str | None = None, **kwargs) -> LLM:
    """`provider=None` 时按角色解析；非 None 时对所有角色强制该 provider（CLI 覆盖）。"""

class MockHeteroLLM:   # provider="mock_hetero"：确定性异构剧本，**离线可验收**
class LiteLLMAdapter:  # provider="litellm"：真调；key 缺失 → 中文 RuntimeError + rc=4
```

- `--provider` 语义变更（**兼容旧行为**）：`mock | litellm | mock_hetero`；不传 = 按角色配置。
- `REVIEWER` 在 `config/models.yaml` 里加 `provider: mock_hetero`（P2 默认），真实 key 到位后改 `litellm` 一行。
- 铁律不破：代码里仍只出现角色名；模型串只在 `config/models.yaml` 与 `llm.py` 适配器内部。

### 3.3 评审位 —— `review.py` / `pipeline.py`（长流）

```python
def run_review(package: TaskPackage, llm, *, provider: str, round_no: int) -> ReviewReport:
    """调 REVIEWER，**全新上下文**（不得传 ARCHITECT 的对话/任务包以外的历史）。"""
def apply_review_to_gate(report: ReviewReport, gate: GateReport) -> GateReport:
    """G8 折算（D3 已冻结，不变）：
       major 且带 criterion → 加入 failing_criteria → NEEDS_FIX
       major 无 criterion / minor / info → 只记 reasons
       解析失败 → 只记 reasons，不改三态（R-5 不变）"""
```

- `ReviewReport.context_id` 必须与同轮 ARCHITECT 调用不同；`store` 留痕（`reviews` 表）。
- fresh context 的落实方式：每次评审**新建** LLM 会话（不传历史 messages），`context_id = f"rev-{round_no}-{attempt}-{uuid4}"`。

### 3.4 同源盲区度量 —— `eval.py`（长流）

种子集 `benchmarks/review/seed.jsonl`，一行一个样本：

```json
{"id":"S001","feature":"order-atp","package":{...六段…},
 "injected_defects":[{"criterion":"AC2","location":"段4 接口契约","kind":"missing_error_code"}],
 "human_verdict":"fail"}
```

```python
def evaluate(seed_path, *, llm, provider: str, baseline_provider: str = "mock") -> ReviewMetrics:
    """两组跑同一批种子：provider（异构）与 baseline_provider（同源），输出四项指标。"""
def render_report(metrics: ReviewMetrics, fmt: str = "json") -> str: ...
```

- 指标定义（必须可复算）：
  - `missed` = 人工标为 `fail` 的样本里，评审给 `pass` 或未在规定 citerion 上命中的条数；
  - `false_alarms` = 人工标为 `pass` 的样本里被评审判 `fail` 的条数；
  - `agreement` = 与 `human_verdict` 一致的样本数 / total；
  - `baseline_*` 为同源 provider 的对应值（对比列）。
- CLI：`kfpa eval review [--seed PATH] [--provider mock_hetero] [--baseline mock] [--out reports/review-eval.json]`。
- **通过判据（P2）**：异构组 `missed <= baseline.missed` 且 `agreement >= baseline.agreement`
  （种子集是确定的，两组结果可复算；若同源已 0 漏检，则要求异构也 0 漏检且误报不增加）。

### 3.5 覆盖率债清偿（P2 硬验收）

- 白名单（D3 R-22.4 不变）：`schemas.py`/`config.py`/`cli.py`/`store/**`/`gates.py`/`pipeline.py`；
  P2 追加 `review.py`/`eval.py`。
- 目标：**白名单行覆盖率 ≥ 80%** 且 `verify_p0+verify_p1+verify_p2` 关键路径全绿（`critical_paths_ok=True`）。
- 归属：谁的白名单模块谁补测试；台架口径由镜鉴核（可由台架生成合并 `coverage.json` 交给 G7）。

### 3.6 报告渲染 —— `ui/**` / `reports/**`（界面）

- `kfpa eval review --out reports/review-eval.json` 之后，可渲染 Markdown / 单文件 HTML 报告：
  两组指标对照表 + 逐样本明细（哪条漏检、哪条误报）+ 结论（是否达到 §3.4 判据）。
- 页面 `ui/eval_page.py` 惰性 import streamlit，不列入验收；渲染函数必须可 headless 调用。

## 4. 验收标准 A18–A22（镜鉴独立验证，clean env）

> 沿用 R-19/R-25/R-27：台架自带 stdio 兜底、剥掉 `KFPA_DB_URL`、测 rc 先落文件、探针做驱动归一。

| 编号 | 断言 | 命令（示例） |
|---|---|---|
| A18 | **按角色 provider**：REVIEWER 走 `mock_hetero`、ARCHITECT/FLASH 仍 `mock`；`--provider litellm` 全局覆盖；未知 provider → 中文 + rc=4 | `python -m kfpa round … --json` + 检查 ledger/reviews 里的 provider |
| A19 | **fresh context**：`reviews.context_id` 与同轮 ARCHITECT 上下文不同，且同轮两次评审 context_id 不同；`fresh_context` 违反时必须被判红 | `python -c "…Store.list_review_reports(1)…"` |
| A20 | **覆盖率达标**：白名单行覆盖 ≥80% 且关键路径全绿 → G7 判 PASS | `pytest --cov=kfpa --cov-report=json` + `verify_p0/1/2` |
| A21 | **同源盲区度量**：种子集跑出四项指标，两组可对比，报告可落盘；边界用例（空集/全漏/全中）有单测 | `python -m kfpa eval review --out reports/review-eval.json` |
| A22 | **评审接入主循环**：`round` dry-run 下 REVIEWER 被调用、结果按 G8 折算（major+criterion → NEEDS_FIX；解析失败不改三态）；`reviews` 表留痕 | `python -m kfpa round … --inject test_fail` |

验证要求同前：命令 + 真实输出片段 + 结论；断言独立；失败指名到模块与接口。

## 5. 依赖与环境

| 项 | 状态 |
|---|---|
| 第二家模型 API key | **暂无**（`list_secrets` 空）→ 真实异构调用不在 P2 验收范围；接线完成即交付 |
| `mock_hetero` provider | 纯标准库实现，离线确定 |
| 覆盖率工具 | `coverage` / `pytest-cov` 已装（P1 装） |
| Docker（A20 无需；真实 PG 对拍仍可用） | 已就绪（29.8.2） |

## 6. 合并顺序与门禁

1. `p2/router`（磐石）→ 2. `p2/review`（长流）→ 3. `p2/eval-ui`（界面）→ 4. `p2/verify`（镜鉴）。
2. 每合一步：镜鉴在 main 上跑 `verify_p0` + `verify_p1` + `verify_p2`；**P0/P1 台架不许回归**。
3. 契约升 1.3 按 R-20/R-21 同批同步台架 `SUPPORTED_CONTRACT_VERSIONS`。
4. `reviews` 表 → `schema_version = "3"`，按 R-21 同批同步 `selfcheck_core.py`（走常量，不写字面量）。

## 7. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 1.0 | 2026-10-16 | 首版冻结：P2 范围、按角色 provider、fresh context、同源盲区度量口径、覆盖率债、A18–A22、四人分工 | Chief |

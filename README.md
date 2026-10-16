# KFPA

**Knowledge-First Pipeline Architecture** —— 知识优先的流水线式多智能体软件生产架构。

> 日式详细设计（詳細設計）开发方式的 AI 化：**人类出原始详细设计，AI 负责设计辅助、转译与零思考执行。**
> 失败的 economic 成本 = token：串行小步滚动、失败回灌重试、按轮快照一键回滚。

## 核心思想

- **人在设计位**：人类做原始详细设计（杠杆率最高的位置），主BOT 只做转译不做架构决策
- **详细设计方式的 AI 化**：主BOT = 詳細設計者助手，Flash = 零思考実装担当，任务包 = 詳細設計書
- **質問票机制**：主BOT 转译时产出歧义报告回人工确认，未关闭的高影响票不得开跑
- **失败经济学**：重试与回滚只烧 token，前提是失败原因回灌下一次任务包
- **知识沉淀带闸门**：置信度门槛 + 隔离区转正，防知识库中毒；失败记录沉淀为回避模式清单
- **一个模型两挡位**：决策开思考 / 执行关思考，模型绑定全配置化（代码只认角色名）

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/design.md](docs/design.md) | **详细架构设计**（设计哲学 / 角色职责 / 分层架构 / 轮次生命周期 / 关键机制规格 / 路线图） |
| [docs/spec.md](docs/spec.md) | 设计规格补充件（任务包模板 / 質問票模板 / 分级重试 / 快车道判据 / 沉淀闸门规则） |
| [docs/tech-stack.md](docs/tech-stack.md) | 技术选型（LiteLLM + Aider + LangGraph + Dify 全开源组合，懒人版/正经版两种部署形态） |
| [diagrams/architecture-v2.html](diagrams/architecture-v2.html) | 架构图（可浏览器直接打开） |
| [config/models.yaml](config/models.yaml) | 模型角色绑定配置（ARCHITECT / REVIEWER / FLASH / EMBEDDING） |

## 快速概览

```
人类设计者（原始详细设计）
   ↑① 質問票（歧义报告）        ↓② 确认放行
主BOT（旗舰挡：设计辅助 / 转译 / 调度 / 回灌）
   ↓
功能开发流水线（串行滚动）：功能N BOT → 任务包 → Flash 执行器 → 代码
   ↓
质量门禁三态：通过 → 按轮快照入库 ｜ 需修复 → 自主重试 ｜ 阻塞 → 升级主BOT
   ↓
知识沉淀闸门 → 知识库（下一轮更好，负知识也入库）
```

## 状态

✅ **P0 最小闭环已交付**（2026-10-16）：设计 → 質問票 → 人工确认 → 六段式任务包 → 执行器（mock）→ 门禁三态 → SQLite 落库 → 按轮快照，全程可**离线**验收（无网络、无 API key）。

✅ **P1 正经版已交付**（2026-10-16）：PostgreSQL 双驱动 + 双库对拍、token 账本、checkpoint 落盘、G7 覆盖率门槛、沉淀闸门、快车道判据、Streamlit 台账。

✅ **P2 评审位异构化已交付**（2026-10-17，main）：评审位接进主循环（G8 折算：`major + criterion` 才阻塞）、异构 provider 可换挡、同源盲区可量化度量、评审报告三形态（Markdown / HTML / 页面）。路线图见 [docs/design.md §8](docs/design.md#8-路线图)。

### 快速开始（clean env，无需 API key）

```powershell
$env:PYTHONPATH="src"                                                # 见 docs/dev/p0-rulings.md R-2
python -m kfpa init --db kfpa.db
python -m kfpa round --design examples/design-r1.md --round 1 --dry-run --db kfpa.db
python -m kfpa tickets list --db kfpa.db                             # 質問票台账
python -m kfpa tickets batch-close --file answers.yaml --db kfpa.db  # 攒批确认（格式见 D1-8）
python -m kfpa round --design examples/design-r1.md --round 1 --dry-run --db kfpa.db
python -m kfpa history --db kfpa.db                                  # 轮次快照
```

退出码（D1-5 / D2 R-1，rc 三分见 D2 R-31）：`0` 通过 · `2` 用法错误 · `3` 等待人工确认 · `4` 依赖/环境（含缺凭据）· `5` I/O 与 DB · `10` 需修复 · `11` 阻塞。

### 验收结果（`main` `eca4837`，clean env 不设 `PYTHONIOENCODING`，PG 独占端口）

| 台架 | 结果 |
|---|---|
| `python scripts/verify_p0.py` | **A1–A10 共 13 条断言 PASS 13 / FAIL 0 / SKIP 0** |
| `python scripts/verify_p1.py` | **A11–A17 共 8 条断言 PASS 8 / FAIL 0 / SKIP 0**（A12 双库对拍在真 PG 上） |
| `python scripts/verify_p2.py` | **A18–A22 共 6 条断言 PASS 6 / FAIL 0 / SKIP 0**（A20 先要求 `pytest` 全绿） |
| `python scripts/selfcheck_core.py` | **FAILED: 无** |
| `python -m pytest tests -q` | **194 passed** |

P2 关键数字：R-32 纯逻辑白名单行覆盖 **90.7%**（1605/1770，门槛 80%）；同源盲区度量 24 样本 —— 异构评审漏检 **0** vs 同源 **12**，一致率 **100% vs 50%**。

设计与裁决文档：[docs/dev/p0-plan.md](docs/dev/p0-plan.md)（施工图与 A1–A10）· [docs/dev/p0-design-addendum.md](docs/dev/p0-design-addendum.md)（D1 工程细节）· [docs/dev/p0-rulings.md](docs/dev/p0-rulings.md)（D2 裁决 R-1~R-32）· [docs/dev/p1-plan.md](docs/dev/p1-plan.md)（D3）· [docs/dev/p2-plan.md](docs/dev/p2-plan.md)（D4 契约 1.3）· 验收报告 [P0](docs/acceptance/p0-acceptance.md) / [P1](docs/acceptance/p1-acceptance.md) / [P2](docs/acceptance/p2-acceptance.md)。

## License

MIT

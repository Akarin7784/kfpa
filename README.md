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

🚧 设计定稿，工程实现中。路线图见 [docs/design.md §8](docs/design.md#8-路线图)。

## License

MIT

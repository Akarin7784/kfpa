# KFPA v2 技术选型（开源优先 · 少写代码版）

> 原则：不造轮子，只写胶水。全系统需要自己写的只有 3 块：LangGraph 图定义、任务包模板、質問票页面，合计约 300–500 行。

## 1. 选型总表

| v2 架构组件              | 首选                                      | 为什么是它                                                               | 备选                 |
| -------------------- | --------------------------------------- | ------------------------------------------------------------------- | ------------------ |
| 模型接入 / 旗舰-Flash 分级路由 | **LiteLLM**                             | 一个代理地址统一所有模型，按 model 名分旗舰/Flash，自带 token 记账、重试、降级——失败经济学要算账，它先把账本搭好 | OpenRouter（托管）     |
| Flash 编码执行器          | **Aider**                               | 每次修改自动 git commit（轮次快照白送）、自带 lint/test 钩子、支持任意开源模型，CLI 可被脚本直接驱动     | OpenHands（沙箱更全但重）  |
| 主BOT 流水线状态机          | **LangGraph**                           | 轮次=图节点，checkpoint=项目记忆持久化，interrupt=質問票/人确认（官方特性，白送），条件边=门禁三态       | Temporal（太重，杀鸡用牛刀） |
| 知识库 + RAG            | **Dify**（自托管）                           | RAG + 工作流 + WebUI + API 全家桶，把设计书/组件文档/AAR 喂进去就能查，API 给主BOT 调        | RAGFlow（文档解析更强但更重） |
| 项目记忆 / 状态库           | **PostgreSQL** + LangGraph Checkpointer | 任务状态、質問票台账、失败记录都是表；正经模式上 PG，懒人模式 SQLite 起步                          | —                  |
| 版本管理 / 回滚            | **Git**（Aider 自动提交 + 轮次 tag）            | 回滚 = `git checkout r{n}`，0 行代码                                      | —                  |
| 测试执行                 | **pytest** + **Docker**                 | 测试从设计规约生成（规约→pytest 断言），容器里跑，脏环境不影响宿主                               | testcontainers     |
| 审查聚合                 | **PR-Agent**（Qodo 开源版）                  | 现成的多维代码评审（正确性/安全/风格），结果喂给主BOT 汇总                                    | 纯 prompt 自评（省钱但弱）  |
| 可观测 / token 账本       | **Langfuse**（自托管）                       | 每轮每模型调用全留痕，重试成本、烧钱曲线直接看仪表盘                                          | LangSmith（非开源自托管）  |
| 人机界面（質問票/批量承认）       | **Streamlit**                           | 質問票台账 + 一键批量确认，约 100 行；懒人也可直接用 IM 机器人推送确认卡片                         | NocoDB（表格视图零代码）    |
| 任务包模板                | **Jinja2**                              | 六段式任务包做成模板，主BOT 只填空                                                 | —                  |
| 运行环境                 | **Docker Compose**                      | Dify / Langfuse / PG / LiteLLM 全有官方镜像，一个 compose 拉起                 | —                  |

## 2. 两种部署形态

### 懒人版（一天跑通，先证明流水线能转）

```
LiteLLM ─→ Aider（Flash 执行 + 自动提交）
   │
LangGraph（SQLite checkpoint）── 轮次状态机 + 質問票 interrupt
   │
Dify ── 知识库 RAG（先只喂设计书和踩坑记录）
```

写什么：LangGraph 图定义（~150 行）+ 任务包模板（1 个 Jinja2）+ 质量门禁判定脚本（~100 行）。

### 正经版（一周内，补齐纪律）

- SQLite → PostgreSQL + Langfuse 仪表盘
- 加 PR-Agent 审查聚合、Streamlit 質問票台账、Docker 测试沙箱
- 沉淀闸门：Dify 里建"待验证"知识库空间，转正规则写在主BOT 的回灌 prompt 里

## 3. 模型建议（开源优先）

| 角色               | 首选                          | 说明                                                                                       |
| ---------------- | --------------------------- | ---------------------------------------------------------------------------------------- |
| 主BOT / 功能BOT（旗舰） | **DeepSeek-V4.1-Flash（开思考模式）** | 与 Flash 同一 API，一个模型两挡位：决策/转译开思考，执行关思考，少管一个 key |
| Flash 执行器        | **DeepSeek-V4.1-Flash（关思考）** | 低延迟低价，重试烧的就是它；LiteLLM 映射 `deepseek/deepseek-v4.1-flash`，按请求带/不带 reasoning 参数换挡；本地备胎 Qwen3-Coder |
| Embedding        | BGE-M3（开源）                  | 中文组件文档检索够用                                                                               |

## 4. 风险与实话

1. **Aider 对弱模型敏感**：好在 DeepSeek 系历来 是 Aider diff 格式支持最好的开源模型之一，V4.1-Flash 风险不大；仍建议第一轮跑通时盯一下 diff 应用成功率，异常再切 OpenHands。
2. **Dify 的代码类知识检索一般**：组件库若以代码片段为主，换 RAGFlow 或直接让主BOT 读 git 仓库。
3. **LangGraph 有学习曲线**（半天到一天），但它的 checkpoint/interrupt 恰好是質問票机制的本体，值得。
4. **同源盲区（一个模型两挡位的代价）**：设计者、执行者、评审者共享同一套训练偏差——主BOT 理解错的设计，Flash 会用同样的方式理解错，评审也可能一起放过。缓解：对抗性评审至少换全新上下文 + 独立角色 prompt；预算宽裕后优先给"评审位"换异构模型（GLM 等），设计/执行同模型可以忍，评审同模型不能忍。
   **→ 债务已配置化**：所有模型绑定收敛到 `kfpa-config/models.yaml`，代码只认角色名（ARCHITECT/REVIEWER/FLASH），换模型 = 改配置文件，零代码。接入异构 API 后按文件内债务清单依次消化。
5. 别用 AutoGen/CrewAI 当骨架：它们擅长"角色聊天"，不擅长"带持久化状态和人工卡点的确定性流水线"——你要的是后者。

## 5. 验证方式

```bash
docker compose up -d          # 拉起 LiteLLM/Dify/PG/Langfuse
python -m kfpa.run --round 1 # 跑第一轮：设计→質問票→确认→编码→门禁
# 预期：git log 出现 r1 快照提交；質問票列表非空；Langfuse 可见本轮 token 消耗
```

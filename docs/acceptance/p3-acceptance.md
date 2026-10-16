# KFPA P3 收口验收报告

> 台架作者：**镜鉴**（`scripts/verify_p3.py` + `tests/**` 的 R-30 搬运）
> 终验执行：**Chief**（合并在 `main` 上、**独占 PG** 跑全量，`2026-10-17`）
> 依据：`docs/dev/p3-plan.md`（D5 契约 1.4）§3 冻结接口、§4 验收 A23–A27、§7 合并门禁；
> 纪律沿用 P0–P2（R-2/R-19/R-20/R-21/R-25/R-27/R-29/R-30/R-31/R-32）＋ P3 新增 R-33–R-37。

---

## 0. 结论

**P3 通过收口**：`verify_p3` **6/6（PASS 6 / FAIL 0 / SKIP 0）**，P0/P1/P2 无回归，`pytest` 全绿。

| 项 | 值 |
|---|---|
| 契约版本 | `CONTRACT_VERSION 1.4`（`schemas.py`，台架支持集同批收 1.4，R-29 升版守卫） |
| schema 版本 | `SCHEMA_VERSION 4`（`0003_fastlane.sql`：`fastlane_requests` + `knowledge_samples`） |
| main | `358ad46`（P3 共 20 笔 PR：#54–#75） |
| `pytest tests -q` | **342 passed**（rc=0；clean env 复跑同样 342） |
| P0/P1/P2 台架 | **13/13 · 8/8 · 6/6（0 SKIP，A12 真 PG 双库对拍）** |
| R-32 纯逻辑白名单覆盖率 | **96.3%（1823/1893）** ≥ 80%（P2 收口基线 90.7%） |
| P3 模块覆盖率（#59 增量门槛） | `fastlane.py` **83.3%**（365/438） · `knowledge.py` **84.5%**（131/155） |
| 全仓行覆盖率（报告项） | 68.6%（含 `ui/**`、`store/postgres.py` 等非门槛文件） |
| 终验环境 | clean env（剥 `KFPA_DB_URL`）＋ **独占 PG**（55440，R-37）· 380.7s |

## 1. 必跑命令

```powershell
$env:PYTHONPATH="src"
python scripts/verify_p0.py                    # 13/13
python scripts/verify_p1.py                    # 8/8（A12 需真 PG）
python scripts/verify_p2.py                    # 6/6
python scripts/verify_p3.py --coverage-out cov.json --json p3.json   # A0 + A23–A27
python scripts/selfcheck_core.py               # FAILED: 无
python -m pytest tests -q                      # 342 passed
# 共享 PG 残留自查（A0 尾项，R-36 后入口已门禁）
python benchmarks/p3_store/check_pg_residue.py            # 0 无残留 / 1 有残留(点名) / 2 用法或非 PG / 4 PG 不可达
```

## 2. A23–A27 逐条结论（main `358ad46` 实测）

| 断言 | 结论 | 实测证据 |
|---|---|---|
| **A0** 不回归 | PASS | 内嵌三段台架 `13/13 + 8/8 + 6/6`；`pytest rc=0`；白名单 96.3% 且 P3 两模块各 ≥80%；契约 1.4 支持集在 p0/p1/p2/p3 四处齐 |
| **A23** 快车道落库与攒批承认 | PASS | `plan` → 登记；`ack`/`batch-ack` 落 `fastlane_requests`；二次承认幂等且**不覆盖**首次 `decided_by/decided_at`；`batch_id` 同批共享；`ESCALATED`/`REJECTED` 不可翻白；`acks` 三种坏形状（缺键 / 元素缺 `request_id` / **非列表**）全部 **rc=2 + 中文**；`schema_version=4`、12/12 列与 5/5 列齐 |
| **A24** 判据与升级 | PASS | 三判据全真 → `pending`；任一为假 → `ESCALATED` 且 `escalations()` 指名（`new_interface`/`new_data_model`/`touched_modules>1`）；**`no_diff_source`（R-23 第三来源 / R-35）不许默认放真** |
| **A25** 沉淀闸门全量 | PASS | 门槛 **0.80** 以下直接 `DROPPED`（不进隔离区）；隔离区只被**后续轮次**复用才转正（同轮不算）；`record_sample(passed=False)` → 候选 `DROPPED` + `knowledge_samples` 留痕 + 回避模式（`FailureRecord.avoid_pattern`）可查；动态抽检率分档 |
| **A26** 负知识回灌 + 双库一致 | PASS | 两侧跑同一序列（失败轮 → 净轮）：`package_hash` SQLite = PG、`feedback` 逐字一致；**R-28 上界**：窗口内无失败时不许再带出更早轮次的 `AVOID[...]` |
| **A27** RAG 检索 | PASS | 默认 `auto` 离线命中且稳定排序、`provider="local"`、`fallback=true`；**显式 `--provider dify` 缺凭据 → rc=4 + 中文（打码）**；HTTP 失败 / 不可达 → rc=5；全程无 key 明文、库里也无 |

## 3. 本轮裁定（P3 收口口径）

| 裁定 | 内容 |
|---|---|
| **R-33** | 沉淀门槛维持 **0.80**（D5 v1.0 的 0.6 是笔误）· A25 闸门入口冻结名（`knowledge.admit` / `record_sample`）· A26 复用 A12 的 PG 夹具（`KFPA_PG_PORT` 默认 55440）· `verify_p3` 的 A0 必须始终绿、A23–A27 的红是派工单 |
| **R-34** | `kfpa.fastlane` 是 `fastlane` 组**唯一 owner**（`check` 一并注册、`impact` 不再注册）· `acks.yaml` schema 冻结 `acks: [{request_id, note?}]` |
| **R-35** | 「无 diff 来源」不许默认真：`judged_fastlane()` 收 `bool(diff_files)`、`escalations()` 首条 `no_diff_source`、`plan()` 转 `ESCALATED` |
| **R-36** | A27 的 rc 三态（`auto`/`local` 回退 rc=0、显式 `dify` 缺凭据 rc=4、调用失败 rc=5）· `acks` 非列表 = rc=2 第三种坏形状 · A0「桩未搬」窗口允许但必须本轮关掉 |
| **R-37** | 终验必须**独占 PG**（A12/A26 共用同一个库，并发跑会读串 → 假红）；报红先 `--only` 复跑再定论 |
| **R-38** | 台架**不许依赖 gitignored 的运行时状态**（`runs/` 是产品行为的一部分、不能删了事，探针必须钉在自己的空 `--workdir` 上）；**隔离与接链要一起断**（如 A23 的来源① 正向对照） |

## 4. 交付物（P3）

- 快车道：`src/kfpa/fastlane.py`（判据 / 登记 / 攒批承认 / 微型规格 / CLI 六子命令）、`fastlane_requests` 落库（SQLite + PG 同签名）
- 沉淀闸门：`src/kfpa/knowledge.py`（门槛 / 隔离区 / 后续轮次转正 / 动态抽检 / 回避模式 + `sample|verdict|report`）
- 检索：`src/kfpa/rag.py`（本地回退 + Dify 惰性适配器 + 打码）
- 界面：`ui/fastlane_page.py`（**真承认**：写库 + 回读 + 幂等重放不冒领 + 防误点）、`ui/knowledge_page.py`（隔离区 / 抽检 / 回避模式）
- 产物：`reports/fastlane-<批次>.{md,html}`、`reports/knowledge-report.{md,html}`（单文件、无 CDN、无外链、可离线打印）
- 台架与测试：`scripts/verify_p3.py`（A0 + A23–A27、R-33–R-37 口径内建）、`tests/test_p3_*.py`（R-30 搬运 + 独立加强）

## 5. 未达成 / 转下期

| 项 | 现状 | 计划 |
|---|---|---|
| Dify 真调 | 未部署，本地回退是验收路径；适配器**接线完成即交付** | 用户提供地址与 key 后真调一次 |
| 真实 LLM / Aider 执行器 | 无 API key，验收走 mock | 接入 key 后逐项验证 |
| `store/postgres.py` 行覆盖 | 非门槛（R-32），PG 覆盖由 A12/A26 双库对拍承担 | 有 Docker 时随 A20b 报告 |
| 快车道「Flash 直跑」 | 承认落库与直跑入口已交付；真实执行器仍为 mock | 接 Aider 后跑真直跑 |
| Langfuse 仪表盘 / 多模型成本优化 | 未做 | 独立排期 |

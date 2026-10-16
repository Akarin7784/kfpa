# KFPA P2 收口验收报告（Chief 在 main 上复跑）

> 台架作者：**镜鉴**（`scripts/verify_p2.py` + `tests/**` 的 R-30 搬运，PR #49）
> 复跑与判定：**Chief**（合并在 main 上跑全量，`2026-10-17`）
> 依据：`docs/dev/p2-plan.md`（D4 契约 1.3）§3 冻结接口、§4 验收 A18–A22、§6 合并门禁；
> 纪律沿用 P0/P1（R-2 运行方式、R-19 stdio 兜底、R-20 升版守卫、R-29 分支纪律、R-30 桩交接）。

---

## 0. 结论

**P2 通过收口**：`verify_p2` **6/6（PASS 6 / FAIL 0 / SKIP 0）**，且 P0/P1 台架与 `pytest` 无回归。

| 项 | 值 |
|---|---|
| 契约版本 | `CONTRACT_VERSION 1.3`（`ReviewReport` / `blockers()` / 异构评审位） |
| 验收台架 | `scripts/verify_p2.py`（A18–A22 共 6 条断言；A20 内置 `pytest` 必须全绿的前置判） |
| main | `eca4837`（P2 五支 PR #39–#43 ＋ #44/#45/#47/#48/#49/#50/#52） |
| `pytest tests -q` | **194 passed** |
| 覆盖率（R-32 纯逻辑白名单） | **90.7%（1605/1770，门槛 80%）** |

## 1. 必跑命令（clean env，不设 `PYTHONIOENCODING`）

```powershell
$env:PYTHONPATH="src"
python scripts/verify_p0.py       # A0 不回归守卫的来源
python scripts/verify_p1.py       # A12 需真 PG（独占端口 55440）
python scripts/verify_p2.py       # A18–A22
python scripts/verify_p2.py --coverage-out cov.json   # A20 合并报告，供 G7 复用
python scripts/selfcheck_core.py  # FAILED: 无
python -m pytest tests -q         # 194 passed
```

## 2. A18–A22 逐条结论（main `eca4837` 实测）

| 断言 | 结论 | 实测证据 |
|---|---|---|
| **A18** 评审位接入与退出码三分 | PASS | `--provider nope` → **rc=2**（argparse 原生拒绝，不劫持）；`--provider litellm` 无凭据 → **rc=4**（不崩栈）；dry-run 一轮 rc=0 且 `reviews` 有 `mock_hetero` 留痕 |
| **A19** 重试必须产生多次**独立 context** | PASS | 注入 `test_fail` → rc=11；第 1 轮 2 条评审，`context_id` 互不相同且形状为 `rev-<round>-<attempt>-<uuid>` |
| **A20** 覆盖率门槛（R-32 口径） | PASS | 纯逻辑白名单 9 文件 90.7% ≥ 80%；同报告喂产品 G7 判 PASS；门槛外文件未被计入；A20b：`store/postgres.py` 0/196，仅报告 |
| **A21** 同源盲区度量 | PASS | 24 样本：异构漏检 **0** / 误报 0，一致率 **100%**；同源 baseline 漏检 **12**、一致率 **50%**；`metrics` 与 `samples` 复算一致；空集/全漏/全中的边界都覆盖 |
| **A22** G8 折算 + 留痕 | PASS | `major + criterion` → `NEEDS_FIX`（阻塞）；`major` 无 criterion → `PASS`（不阻塞）；解析失败不改三态；`reviews` 十列齐全，第 1 轮 2 行 |
| A0 | PASS | P0 台架 13/13 不回归 |

## 3. 本轮裁定（收口口径）

- **R-31 退出码三分**：未知值 **2**（argparse 原生，不许劫持退出码）· 合法但缺依赖/凭据 **4** · I/O 与 DB **5**。按异常类型落地：`ImportError`/`RuntimeError` → 4，`OSError`/`sqlite3.Error` 与其它异常 → 5。
- **R-32 覆盖率门槛口径**：门槛只算纯逻辑白名单（`schemas` / `config` / `cli` / `gates` / `pipeline` / `review` / `eval` / `store/__init__.py` / `store/sqlite.py`）；`store/postgres.py` 排除、降为 A20b 报告项（clean env 无 Docker 恒 0%），PG 覆盖由 **A12 双库对拍**承担。
- **合并纪律（本轮真踩到的坑）**：PR 基点落后于 main 时，squash 合并会留下「新实现 + 旧期望」的假绿真红（#49 的 rc 期望早于 #48）→ 合前 rebase、合后必须跑全量台架。**这条已写进进度地图的工程护栏。**
- **台架自身缺口**：A20 原先只看覆盖率 JSON 是否存在，`pytest` 红了照样 PASS；现补前置硬判 `pytest rc=0`（#52 修复）。

## 4. 未达成 / 转下期

| 项 | 现状 | 计划 |
|---|---|---|
| 真实适配器 | LiteLLM（需 key）/ Aider / Dify 均未在真机跑通，P0–P2 验收走 mock | 接入 key 后逐项验证 |
| `store/postgres.py` 行覆盖 | 0/196（clean env 无 Docker），已按 R-32 排除出门槛 | A12 双库对拍承担；有 Docker 时 A20b 报告 |
| Streamlit 台账交互 | headless 冒烟通过，不列入验收 | 可选增强 |
| 知识库 RAG | EMBEDDING 角色占位，Dify 未接 | P3 / 独立排期 |

# KFPA P1 独立验收报告

> 负责人：**镜鉴**（分支 `p1/verify`，工作树 `workspace\kfpa-jingjian`）
> 依据：`docs/dev/p1-plan.md`（D3）§3 冻结接口、§4 验收 A11–A17、§6 合并门禁；
> 纪律沿用 P0（R-2 运行方式、R-19 独立脚本 stdio 兜底、R-20 升版守卫）。
> 本报告只做**独立验证**，不改任何人写的实现。

---

## 0. 当前状态

| 项 | 值 |
|---|---|
| 验收台架 | `scripts/verify_p1.py`（A11–A17 ＋ **A0 = P0 不回归守卫**，共 8 条断言） |
| 契约版本 | `CONTRACT_VERSION 1.2`（`LedgerEntry` / `KnowledgeCandidate` / `CandidateStatus` / `CoverageReport`） |
| 最近一次全量运行 | 2026-10-16 @ `main@60b786d`（P1 模块未交付）：**8 条断言 PASS 1 / FAIL 7**，唯一 PASS 是 A0（P0 台架 13/13 无回归）；A11–A17 的 FAIL 全部指名「缺哪个文件、归谁写」 |
| 判定 | **P1 未交付**：`p1/store`（磐石）、`p1/pipeline`（长流）、`p1/ui`（界面）尚未落分支 |
| 契约层测试 | `tests/test_contract_p1.py`（1.2 三件新结构）；`pytest tests -q` → **40 passed** |

台架原则与 P0 一致：缺件按 FAIL 报、逐条指名到文件与责任人；SKIP 只用于环境不具备（A12 的 Docker），
且必须在报告里显式可见。

---

## 1. 怎么跑

```powershell
$env:PYTHONPATH="src"
python scripts/verify_p1.py                     # 全量（A0 + A11–A17）
python scripts/verify_p1.py --only A12          # 只跑双库对拍
python scripts/verify_p1.py --skip-pg           # 本机无 Docker 时跳过 A12（显式 SKIP）
python scripts/verify_p1.py --json out.json
python -m pytest tests -q                       # 含 1.2 契约复算
```

合并门禁（D3 §6.2）：每合一步，在 main 上跑 `verify_p0.py` ＋ `verify_p1.py` 全量，
**P0 台架 13/13 不许回归** —— 这条已由 A0 直接钉进台架（A0 会拉起 `verify_p0.py` 并解析其 JSON 证据）。

---

## 2. A11–A17 逐条结论

| 台架 id | 验收项 | 责任 | 当前结论 |
|---|---|---|---|
| A0 | P0 不回归（`verify_p0.py` clean env 仍 13/13）＋ 契约版本 1.2 | 镜鉴 | **PASS** |
| A11 | token 账本：`ledger` 落表、`kfpa ledger --round N [--json]` 合计、`model_ref` 来自 config、`estimate_tokens` 确定性 | 长流 + 磐石 | **FAIL**（缺 `ledger.py` / `store/ledger` 表 / `ledger` 子命令） |
| A12 | 双库对拍：同一命令在 SQLite 与 PG(55432) 跑出同一断言集（七表 + `ledger` + `knowledge`），票号/快照一致 | 磐石 + 长流 | **FAIL**（缺 `docker-compose.pg.yml` / `migrations/0001_init.sql` / `store/postgres.py`） |
| A13 | checkpoint：`SqliteSaver` 落盘、`thread_id=kfpa-r{n}`、同轮重跑不重复开票且票面状态保留 | 长流 | **FAIL**（缺 checkpoint 落盘与 thread_id） |
| A14 | G7 覆盖率：`CoverageReport.passed()` 边界（0.79 否 / 0.80 是 / 关键路径红否）＋ 不达标 → `NEEDS_FIX` 且 `failing_criteria` 含未达标文件 | 长流 | **FAIL**（`gates.evaluate` 未接 `coverage=` 参数） |
| A15 | 沉淀闸门：<0.80 直接 `dropped`、≥0.80 进隔离区、复用才转正、`avoid` 不进正区、抽样率 `max(0.05, 0.20-0.05n)` | 长流 + 磐石 | **FAIL**（缺 `knowledge.py` 与 store 的 candidate 方法） |
| A16 | 快车道三判据：全真 `fast_lane=True`；动 `migrations/**`、`schemas.py` 或跨 2 模块 → `False` + 边界清单；CLI 不通过时提示「升级回完整设计回合」 | 长流 | **FAIL**（缺 `impact.py` 与 `fastlane` 子命令） |
| A17 | Streamlit headless：渲染函数可调用并**返回表格数据**；`main()` 不崩；缺包中文提示 + rc=4 | 界面 | **FAIL**（P0 的 `streamlit_app` 只有 `main()` 返回 `int`，没有返回表格数据的渲染函数） |

---

## 3. 台架自身的两处自查

1. **A17 差点成为假绿**：首版只断言「渲染函数返回非 None」，结果 P0 的 `main()` 返回 `int=0` 就判绿。
   按 D3 §3.7 收紧为「必须返回**表格数据**（list/dict/tuple）」，并要求拆出纯渲染函数 —— 现在正确判红，
   并给了界面可执行的整改口径。
2. **A12 的 SKIP 必须可见**：本机无 Docker 或 `--skip-pg` 时显式 SKIP（不算通过），
   并在报告与 JSON 里可见；有 Docker 时才真起栈、真对拍，收尾只关「本次自己起的」栈（不碰别人正在用的）。

---

## 4. 复核发现（P1 待办）

1. **【重要 · `gates.evaluate` 需要新增 `coverage=` 形参】** D3 §3.3 要求 G7 参与三态判定，
   而 P0 的签名是 `evaluate(round_no, package, exec_result, store, attempt=1)`；A14 会以
   `TypeError` 判红并指名这一点 —— 长流记得把可选参数加进签名并保持 P0 调用兼容。
2. **【重要 · `Streamlit` 渲染函数要单独拆出】** 现在只有 `main()`（返回退出码）；
   headless 验收与 Streamlit 真跑都依赖一个不依赖浏览器、返回表格数据的函数。
3. **【建议 · 覆盖率自证】** A14 会额外跑一次仓库自身覆盖率并记录数值；低于 80% 时只给建议不判红
   （G7 是门禁行为，不是本轮仓库水平）。

---

## 5. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| 0.1 | 2026-10-16 | 首版：`scripts/verify_p1.py`（A0 + A11–A17）；`tests/test_contract_p1.py`（契约 1.2 复算，pytest 40 passed）；全量 PASS 1 / FAIL 7（P1 未交付） | 镜鉴 |

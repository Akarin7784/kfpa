# examples 用法（长流）

本目录是 P0 离线验收的输入样本，**不参与任何网络调用**。机读行格式见 D1-7。
运行方式统一 `PYTHONPATH=src`（D2 R-2，不做 `pip install -e .`）。

## 1. 跑一轮完整生命周期（A2）

```powershell
$env:PYTHONPATH="src"
python -m kfpa init --db t.db
python -m kfpa round --design examples/design-r1.md --round 1 --dry-run --db t.db
```

- `--dry-run`（默认）走 `MockExecutor`：确定性产出 `<workdir>/package.md`、`<feature>.mock.txt`、`result.md`
- 默认 workdir 是 `./runs/round-1`（D1-10），可用 `--workdir` 覆盖
- 退出码（D2 R-1，由 `cli.py` 映射）：`0` PASS｜`2` 用法错误｜`3` 等待人工确认（未关闭高影响票）｜`4` 依赖/环境｜`5` I/O｜`10` NEEDS_FIX｜`11` BLOCKED

`design-r1.md` 的 QA 行刻意都是 MEDIUM/LOW，所以默认能跑到 PASS。

## 2. A3：高影响票拦停（退出码 3，纯 CLI 可复现）

D2 R-10 的注入口径：

```powershell
python -m kfpa round --design examples/design-r1.md --round 2 --dry-run --inject high_ticket --db t.db
```

- 期望：**退出码 3**，`tickets list` 能看到升为 HIGH 的票，`task_packages` / `gate_reports` / `snapshots` 不新增；
- 人工攒批确认后放行（`tickets` 子命令由界面 `p0/ui` 提供）：

```powershell
python -m kfpa tickets list --db t.db
python -m kfpa tickets close R2-001 --answer "A：下单即占用（库存数量口径按 A）"
python -m kfpa round --design examples/design-r1.md --round 2 --dry-run --db t.db   # 期望 0
```

`examples/design-r1-high-ticket.md` 是等价备选：设计文本里自带一条 `I: HIGH` 的票，
不依赖 `--inject high_ticket`（适合 `tickets` 子命令还没就绪时用 store API 造票复现）。

## 3. A4：失败注入与三态闭环

```powershell
python -m kfpa round --design examples/design-r1.md --round 3 --dry-run --inject test_fail --db t.db --json
```

- 第 1 次尝试：门禁 `NEEDS_FIX`（`error_category=test_fail`，`failed_criteria=[首条断言 cid]`）
- 同类第 2 次失败：门禁 `BLOCKED` + `escalated=True`（G5，`same_reason_count=1`），**退出码 11**
- 第 2 次尝试的任务包 `feedback` 段必须含第 1 次的 `AVOID[test_fail] ...` 行（D1-11 + D2 R-4，`--json` 里可见）

`--inject` 值域（D2 R-10）：`none`｜`high_ticket`（磐石在 mock 剧本里消化，不进执行器）｜`apply_fail`｜`test_fail`（后两者由 `run_round(injection=...)` 转给 `MockExecutor`）。

## 4. 门禁与执行器的唯一约定

执行器在 `ExecResult` 里回填：
- `error_category`：`apply_failed | compile | test_fail | missing_section | unmapped_criterion | other`
- `failed_criteria`：未通过断言的 cid 列表
- `results`：验收断言 cid → 是否通过（门禁 G4 用它判「断言未映射」）

`gates.evaluate()` 按 D1-3 的 G1→G6 顺序短路判定，全为纯函数，不调用任何模型（D2 R-5）。

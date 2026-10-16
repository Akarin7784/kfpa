# examples 用法（长流）

本目录是 P0 离线验收的输入样本，**不参与任何网络调用**。机读行格式见 D1-7。

## 1. 跑一轮完整生命周期

```
python -m kfpa init --db t.db
python -m kfpa round --design examples/design-r1.md --round 1 --dry-run --db t.db
```

- `--dry-run`（默认）走 `MockExecutor`：确定性产出 `<workdir>/package.md`、`<feature>.mock.txt`、`result.md`
- 默认 workdir 是 `./runs/round-1`（D1-10），可用 `--workdir` 覆盖
- 退出码（D1-5）：`0` PASS｜`10` NEEDS_FIX｜`11` BLOCKED｜`3` 存在未关闭高影响票

`design-r1.md` 的 QA 行刻意都是 MEDIUM/LOW，所以默认能跑到 PASS。

## 2. A3：高影响票拦停（退出码 3）

```
python -m kfpa round --design examples/design-r1-high-ticket.md --round 2 --dry-run --db t.db
```

- 期望：**退出码 3**，stderr 含 `[KFPA-BLOCKED]`，`task_packages` / `gate_reports` / `snapshots` **不新增**；
- 攒批确认后放行：

```
python -m kfpa tickets list --db t.db
python -m kfpa tickets close R2-002 --answer "A：下单即占用（库存数量口径按 A）"
python -m kfpa round --design examples/design-r1-high-ticket.md --round 2 --dry-run --db t.db
```

- 期望：退出码 0，且已关闭的票不会被重跑冲掉。

也可以完全不用示例文件，直接按冻结契约造票（`QATicket(impact=TicketImpact.HIGH)`）再跑同一轮。

## 3. A4：失败注入与三态闭环

```
python -m kfpa round --design examples/design-r1.md --round 3 --dry-run --inject test_fail --db t.db --json
```

- 第 1 次尝试：门禁 `NEEDS_FIX`（G2/G3，`error_category=test_fail`，`failed_criteria=[A1]`）
- 同类第 2 次失败：门禁 `BLOCKED` + `escalated=True`（G5），**退出码 11**
- 第 2 次尝试的任务包 `feedback` 段必须含第 1 次的 `AVOID[test_fail] ...` 行（D1-11，`--json` 里可见）

`--inject` 档位（D1-7）：`none | apply_fail | test_fail`。

## 4. 门禁与执行器的唯一约定

执行器在 `ExecResult` 里回填：
- `error_category`：`apply_failed | compile | test_fail | missing_section | unmapped_criterion | other`
- `failed_criteria`：未通过断言的 cid 列表
- `results`：验收断言 cid → 是否通过（门禁 G4 用它判「断言未映射」）

`gates.evaluate()` 按 D1-3 的 G1→G6 顺序短路判定，全为纯函数，不调用任何模型。

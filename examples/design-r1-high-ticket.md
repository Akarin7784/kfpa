# 示例原始设计（A3 验收专用 · 含 1 张 HIGH 票）

> 用途：验证硬规则 —— 存在未关闭高影响票时不得生成任务包（退出码 3）。
> 内容与 `examples/design-r1.md` 相同，只在末尾多一条 `I: HIGH` 的机读票。
> 复现：`python -m kfpa round --design examples/design-r1-high-ticket.md --round 1 --dry-run --db t.db`
> 看到退出码 3 后，用 `tickets list` 找出 HIGH 票并 `tickets close`，再重跑即应退出码 0。

FEATURE: order-atp
GOAL: 实现门店订单可承诺量校验接口 POST /orders，不足时给出替代建议
AC: 可承诺量 = 可用库存 − 已占用库存（不含在途），计算口径可复算
AC: 可承诺量不足时返回 shortages 列表，且不修改任何库存记录
BOUND: qty 为正整数，上限 9999
IF: POST /orders 出参 {ok, order_code, shortages:[{sku, available}]}
NO: 不改动库存表结构与既有入库接口

## 1. 背景

门店订货系统需要在提交订单时做「可承诺量」判断，避免超卖。

## 2. 机读質問票

QA: §2.2 可承诺量公式 | A: 可用库存 − 已占用库存（不含在途） | B: 可用库存 − 已占用库存 − 在途 | P: A：在途尚未入库 | I: MEDIUM
QA: §2.3 占用口径（下单即占用 vs 拣货才占用） | A: 下单即占用 | B: 拣货才占用 | P: 涉及库存数量口径，须人工确认 | I: HIGH

# 示例原始设计（dry-run 输入 · 第 1 轮）

> 用途：P0 离线验收输入（`python -m kfpa round --design examples/design-r1.md --round 1 --dry-run`）。
> 机读行格式见 `docs/dev/p0-design-addendum.md` D1-7：MockLLM 不推理，只抽 `FEATURE/GOAL/AC/BOUND/IF/NO/QA` 行。
> 本文件的 QA 行**刻意都是 MEDIUM/LOW**，让默认 dry-run 能跑完整轮；要验 A3 拦停请用
> `examples/design-r1-high-ticket.md`（多一条 `I: HIGH` 的票）。

FEATURE: order-atp
GOAL: 实现门店订单可承诺量校验接口 POST /orders，不足时给出替代建议
AC: 可承诺量 = 可用库存 − 已占用库存（不含在途），计算口径可复算
AC: 可承诺量不足时返回 shortages 列表，且不修改任何库存记录
AC: 同一 SKU 并发提交时只允许一单成功，另一单返回冲突
BOUND: qty 为正整数，上限 9999
BOUND: 并发用行锁串行化同 SKU，锁等待超时 3s
BOUND: 幂等键 = store_code + order_time + sku 列表哈希
IF: POST /orders 入参 {store_code, items:[{sku, qty}], order_time}
IF: POST /orders 出参 {ok, order_code, shortages:[{sku, available}]}
IF: 库存不足返回 409 + code=ATP_SHORTAGE；参数非法返回 422 + code=BAD_REQUEST
NO: 不改动库存表结构与既有入库接口
NO: 不引入新的第三方依赖（缓存用标准库实现）

## 1. 背景

门店订货系统需要在提交订单时做「可承诺量」判断，避免超卖；配送中心按波次拣货。

## 2. 功能描述

1. 门店提交订单后，系统计算每个 SKU 的可承诺量并返回。
2. 可承诺量 = 可用库存 − 已占用库存。（**在途是否计入占用未说明**）
3. 若可承诺量不足，接口返回失败，并给出可替代 SKU 的建议。
4. 结果要缓存，缓存过期后重新计算。（**过期口径未说明**）
5. 并发提交同一 SKU 时，只允许一单成功。

## 3. 接口（初稿）

- `POST /orders`：入参 `{store_code, items: [{sku, qty}], order_time}`
- 出参 `{ok, order_code, shortages: [...]}`
- 失败时给出错误码。

## 4. 非功能要求

- 响应要快。
- 数据要准。
- 库存数据每天与 ERP 对账一次。

## 5. 备注

- 门店与配送中心的时区可能不同。
- 单位问题等业务方确认。

## 6. 机读質問票（MockLLM 抽取；真实模型走同一 JSON 结构）

QA: §2.2 可承诺量公式 | A: 可用库存 − 已占用库存（不含在途） | B: 可用库存 − 已占用库存 − 在途 | P: A：在途尚未入库，不应占用可承诺量 | I: MEDIUM
QA: §2.4 缓存过期 | A: 固定 TTL 60 秒 | B: 按 SKU 变更事件失效 | P: B 更准，但需要事件源；P0 可先 A | I: LOW
QA: §3 错误码 | A: 统一 409 + code 字段 | B: 每种不足原因一个 HTTP 码 | P: A：前端统一处理 | I: LOW
QA: §5 时区 | A: 统一按配送中心时区（UTC+8） | B: 按门店本地时区 | P: A：跨时区一致性更重要 | I: MEDIUM
QA: §4 对账 | A: 每日 02:00 固定时点对账，容差 0.5% | B: 库存变动即对账 | P: A：批量对账成本低 | I: LOW

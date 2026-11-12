# 指标口径（算之前先对号）

> 每个指标都写清"分子/分母/过滤条件"。**口径不同答案不同，回答时必须写明用的是哪一条。**

## 销量 / 销售额

| 指标 | 口径 |
|---|---|
| 销量 | `SUM(sales_daily.quantity)`，过滤 `deleted=0` |
| 销售额 | `SUM(sales_daily.amount)`，同上 |
| 日均销量 | 指定窗口内 `SUM(quantity) / 窗口天数`（**分母是自然日数，不是有数据的天数**） |

**时间窗口三选一，必须写明：**
| 说法 | 区间 |
|---|---|
| 自然月 | `sales_date >= '2026-11-01' AND sales_date < '2026-12-01'` |
| 本月至今 | `>= 月初 AND <= 今天`（今天是上下文给的当前日期） |
| 滚动 30 天 | `>= DATE_SUB(今天, INTERVAL 30 DAY)` |

⚠️ **不完整月不能和完整月直接比**（会得出 "-61%" 这种假结论）。要对比就用同窗口（如 11-01~11-12 vs 10-01~10-12）。

## 占比 / 份额

```
某维度占比 = 该维度 SUM(amount) / 全局 SUM(amount) × 100
```
- 分母必须**同一时间窗口、同一过滤条件**
- 结果四舍五入到 2 位；总和可能不等于 100%，要说明

## 目标达成率

```
达成率 = 实际销售额 / 目标销售额 × 100
```
- 目标：`sales_target`，取 `target_month` 对应的**整月值**
- 实际：本月至今的实际值
- **本月未结束时，达成率天然偏低**，必须同时给"时间进度"（已过天数 / 当月总天数）做参照
- 维度对齐：`dimension='channel'` 时用 `dimension_value = sales_daily.channel`；`'category'` 时对 `product.category`

## 库存覆盖 / 周转天数

```
覆盖天数 = 当前库存 / 日均销量     （日均销量取近 30 天）
周转最快 = 覆盖天数最小
```
- 分母为 0（近期无销量）→ **单独列出，不能当"覆盖天数无穷大"混进排序**
- 一个商品一条库存记录（按 `product_id`），不要再按仓库分组

## 交付周期（采购）

```
交付周期 = actual_delivery_date − DATE(order_time)   （自然日）
```
- **只统计 `status=4 已入库` 且 `actual_delivery_date IS NOT NULL` 的单**
- 供应商归属：默认按 `purchase_order.supplier_id`（主导供应商），如需按明细供应商须说明

## 在途时长（物流）

```
在途天数 = actual_arrival_time − ship_time
```
- 只算已签收的；未签收的单独统计"超期未签收"：
  `actual_arrival_time IS NULL AND DATEDIFF(今天, DATE(estimated_arrival_time)) > 3`

## Top-N

- 明确是"销量 Top-N"还是"金额 Top-N"，两者排序结果常不同
- 并列要说明（`RANK` vs `ROW_NUMBER`：并列时要不要都给）

## 奖金

- `bonus_rule` 区间是**左闭右开** `[achieve_min, achieve_max)`，最高档 `achieve_max = NULL`
- 达成率落在哪档就给对应 `bonus_rate`

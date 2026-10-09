---
name: inventory-metrics-sop
description: >
  库存口径与物流监控的操作规范：可用量/可售天数怎么算、超期怎么判、结果怎么组织。
  涉及库存数量、补货建议、在途与超期物流时加载本技能。
---

# 库存口径 & 物流监控 SOP

> ⚠️ 本手册的口径**只允许用下列字段**。ERP_OPENCLAW 的 `inventory` 表**没有**
> `reserved`（预留量）/ `in_transit`（在途量）字段 —— **不要引用它们，也不要凭经验推算**。
> 需要"在途"信息时，只能通过 `logistics_search` 查已发货未签收的运单，并按下面 §2 的口径说明。

## 1. 库存口径（回答时必须遵守）

| 指标 | 口径 | 数据来源 |
|---|---|---|
| **可用库存** | `current_quantity`（本系统不单独维护预留量） | `inventory_query` |
| **安全库存** | `safety_stock` | `inventory_query` |
| **预警阈值** | `product.stock_warning_value`（更严格的一档） | `inventory_query` 的 `stockWarningValue` |
| **预警判定** | `current_quantity < safety_stock` | `inventory_query(only_warning=true)` |
| **严重不足** | `current_quantity <= stock_warning_value` | 同上（两档口径与 `inventory_warning_view` 一致） |
| **日均销量** | 近 30 天销量之和 ÷ 30 | `inventory_coverage` |
| **可售天数** | 可用库存 ÷ 日均销量 | `inventory_coverage` |
| **建议补货量** | `日均销量 × 30 − 可用库存`（补足约一个月），结果小于 0 则记 0 | `inventory_coverage` |
| **覆盖状态** | 紧急 < 7 天 / 偏低 < 14 天 / 充足 | `inventory_coverage` |

> ⚠️ **后四个指标一律用 `inventory_coverage` 工具取，不要自己拉日销明细再算。**
> 该工具已把口径固定在里面；自己算会出现「这次 14 天阈值、下次 30 天」的漂移。

**补货触发条件（满足任一即可建议补货）**
- `current_quantity < safety_stock`
- 可售天数 < 14 天

## 2. 物流口径

| 指标 | 口径 |
|---|---|
| **在途** | `ship_time` 有值 **且** `actual_arrival_time` 为空 |
| **超期未签收** | `actual_arrival_time` 为空 **且** `estimated_arrival_time < 当前时间`（用 `logistics_search(overdue_only=true)` 直接查） |
| **在途天数** | 当前时间 − `ship_time` |
| **是否准时** | `actual_arrival_time <= estimated_arrival_time` 为准时 |
| **承运商时效** | 准时率 / 平均在途天数 / 超期单量 → 用 `logistics_performance` |

> 「哪家承运商准时率低、平均在途几天、超期多少单」这类**聚合结论一律调 `logistics_performance`**，
> 不要自己按上面的口径逐单累加 —— 易错且白烧 token。

`status` 字段含义按 ERP 约定解读，**不要自行编造状态码含义**；不确定时只报数值。

## 3. 分析流程（4 步）

1. **定位对象**：`product_search` / `product_query` 找到商品（拿 `productId`、`name`、`unit`）
2. **拿库存**：`inventory_query(product_id=...)` 看库存与库位；或用 `inventory_warning` 直接看全局预警
3. **算覆盖**（需要补货建议时）：调 `inventory_coverage(product_id=...)`
   —— 日均销量、可售天数、建议补货量、覆盖状态由它按固定口径给出，**不要自己算**
4. **看物流**（物流监控任务）：明细用 `logistics_search`；聚合指标用 `logistics_performance`

## 4. 回答格式

1. **先给结论数字**，再列明细（先说"3 个 SKU 低于安全库存"，再逐条列）
2. 库存明细**按商品 + 库位分行**
3. **必须注明口径**：是可用库存还是含在途；日均销量取的是哪个窗口
4. 给补货建议时**同时给出：当前库存 / 日均销量 / 可售天数 / 建议补货量**，不要只给一个数

## 5. 常见错误（别犯）

- ❌ 引用 `reserved` / `in_transit` / `on_hand` 等本系统不存在的字段
- ❌ 把「可用库存」和「含在途」混为一谈
- ❌ 没有销量数据就给出可售天数或补货量（应先说明"缺少销量数据"）
- ❌ 超期判定用 `ship_time` 而不是 `estimated_arrival_time`

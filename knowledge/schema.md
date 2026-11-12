# 表结构与关联关系

> 只列业务含义和易错点，完整列定义用 `sql_describe_table` 查。

## 主数据

| 表 | 说明 | 关键点 |
|---|---|---|
| `product` | 商品（75 个 SKU） | `category` 是中文品类；`supplier_id` 是**主供应商**（与 `product_supplier` 不同口径） |
| `supplier` | 供应商（36 家） | `deleted=1` 软删 2 家；`status=0` 停用 2 家 |
| `customer` | 客户（40 家） | `customer_type` 1/2/3 = KA商超/经销商/电商 |
| `user` | 系统用户 | 与业务分析基本无关 |

## 商品 ↔ 供应商（多对多）

| 表 | 说明 |
|---|---|
| `product_supplier` | 报价关系：`quote_price`、`lead_days`、`moq`、`is_primary`（1=主供应商） |

**两种供货口径，答案不同，必须先确认：**
- `product.supplier_id` → 商品的**主供应商**（一个商品一条）
- `product_supplier` → **全部供货关系**（一个商品可多家）

## 库存与物流

| 表 | 说明 | 关键点 |
|---|---|---|
| `inventory` | 库存（75 行，一个商品一条） | 低库存判定：`current_quantity < safety_stock` |
| `logistics` | 运单（152 条，一单可多条） | 未签收 = `actual_arrival_time IS NULL` |

## 采购

| 表 | 说明 | 关键点 |
|---|---|---|
| `purchase_order` | 采购单（295 单） | `supplier_id` 是**主导供应商**（按明细最大小计归集） |
| `order_detail` | 明细（862 行） | `subtotal` 是**生成列**，INSERT 时不能传 |
| `product_price_log` | 调价记录 | 只有 7 条 |

## 销售

| 表 | 说明 | 关键点 |
|---|---|---|
| `sales_daily` | 日销（约 6300 行） | 粒度 = 商品 × 渠道 × 仓库 × 日期 |
| `sales_target` | 月度目标 | `target_month` 是 `YYYY-MM` 字符串；`dimension` = channel / category |

## 奖金与字典

| 表 | 说明 |
|---|---|
| `bonus_rule` | 达成率区间 → 奖金系数，**左闭右开**，最高档上限 NULL |
| `meta_dict` | 表用途 + 枚举含义字典（可 SQL 直查） |

## 视图（可直接查）

- `inventory_warning_view`：低库存预警（已算好 `stock_status`）
- `order_detail_full_view`：明细 + 商品 + 供应商，一次 join 到位

## 枚举对照

| 字段 | 取值 |
|---|---|
| `supplier.status` | 1=合作中，0=已停用 |
| `supplier.credit_rating` | A=优秀，B=良好，C=一般，D=较差（当前无 D） |
| `purchase_order.status` | 1待审核 / 2已审核 / 3采购中 / 4已入库 / 5已取消 |
| `logistics.status` | 1待发货 / 2运输中 / 3已签收 / 4异常 |
| `customer.customer_type` | 1=KA商超，2=经销商，3=电商 |
| `customer.discount_level` | 1/2/3 = 折扣力度由大到小 |
| `product_supplier.is_primary` | 1=主供应商，0=候选 |
| 所有表 `deleted` | 0=有效，1=已删除 |

## 常用关联路径

```
商品 → 品类        product.category
商品 → 供应商      product.supplier_id（主） / product_supplier（全部）
销量 → 商品        sales_daily.product_id = product.id
采购 → 明细        purchase_order.id = order_detail.order_id
采购 → 供应商      purchase_order.supplier_id（主导） 或 明细→product→product_supplier
库存 → 商品        inventory.product_id = product.id（一个商品一条）
物流 → 采购单      logistics.order_id = purchase_order.id
目标 ↔ 实际        sales_target.dimension_value = sales_daily.channel（或 product.category）
```

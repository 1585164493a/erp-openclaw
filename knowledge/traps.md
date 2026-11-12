# 已知陷阱清单（写 SQL 前必读）

> 这 8 条是实测踩过的坑，每条都附"错在哪 / 怎么改"。

## 1. 软删除

| 错 | 对 |
|---|---|
| `SELECT * FROM supplier` | `... WHERE deleted = 0` |

几乎所有表都有 `deleted`。**join 的每一张表都要各自过滤**，只过滤主表不够。

## 2. 枚举没翻译

| 错 | 对 |
|---|---|
| 回答"供应商 status 为 1" | "合作中 32 家" |

数字对业务方没有意义。参照 `schema.md` 的枚举表。

## 3. 多对多 join 放大行数

`product` × `product_supplier` × `supplier` 是三表关系，直接 join 会把商品放大 2~4 倍。

**要计数就先聚合再 join**：

```sql
-- 对的写法：先按商品聚合供应商数
SELECT p.category, COUNT(*) n
FROM (SELECT product_id, COUNT(DISTINCT supplier_id) c
      FROM product_supplier WHERE deleted=0 GROUP BY product_id) t
JOIN product p ON p.id = t.product_id ...
```

## 4. 两种供应商口径混用

- `product.supplier_id`：商品的**主供应商**（一商品一条）
- `product_supplier`：**全部供货关系**（一商品多家）

"哪些商品有超过 2 家供应商" → 必须用 `product_supplier`；
"这个商品谁供货" → 两者都行，但要说明用哪个。

## 5. 时间窗口想当然

- "最近" → 没说清就按滚动 30 天，并**在回答里写明**
- "上个月" → 自然月还是滚动 30 天？**先问或写明**
- 不完整月 vs 完整月对比 → 得同窗口比，否则结论是假的

## 6. 生成列

`order_detail.subtotal` 是**数据库生成列**，INSERT/UPDATE 时不能传值，否则报错。

## 7. 分母为 0

- 日均销量为 0 时，"库存能卖几天" = 无穷大 → **单独列，不要混进排序**
- 占比的全局分母为 0（该窗口无销售）→ 直接说"该窗口无数据"

## 8. 一次性拉全量明细

`order_detail` 有 862 行、`sales_daily` 有 6300 行。

**要汇总就直接聚合**（`GROUP BY` / `SUM`），不要 `SELECT *` 拉回来自己数 —— 会被截断（LIMIT）且很慢。

## 附：查询前自检 4 问

1. 每张表都加了 `deleted = 0` 吗？
2. 时间窗口写死了吗？和用户说的是一回事吗？
3. 枚举值翻译成业务含义了吗？
4. 有没有 join 造成行数放大（尤其是多对多）？

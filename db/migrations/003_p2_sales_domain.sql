-- ============================================================================
-- P2 销售域（2026-09-17） ERP_OPENCLAW
-- 背景：销售分析子 Agent（销量异常预警与分析）所需的销售域数据
--      在 motorparts_db 中完全不存在（只有采购域：purchase_order/order_detail）。
--       本迁移新建 sales_daily 日销表并灌入 30 天演示数据。
-- 设计：维度对齐 ERP_OPENCLAW 现有口径 —— part.id + inventory.warehouse_location，
--       不引入 AutoChain 的 sku/warehouse 文本维度，避免同库两套商品口径。
-- 数据：确定性伪随机（非 RAND()），同样输入永远同样结果，可复现、可 review。
--       基线日销 base_qty = 15 + ((part_id*37 + n*17) % 11)，即 15~25 —— 刻意压窄：
--       日销异常检测用「自身均值 × 比例」做阈值，基线方差过大会把正常低销量日
--       误判成骤降（曾用 8~30，阈值 avg*0.5 会误报 224 条）。
--       内置两类异常，且任何 part 不会同时命中两类（id 需同时被 11 和 7 整除，即 77 的倍数，
--       而 part.id ≤ 67）：
--         · part_id % 11 = 0 且 n∈[3,6]   → 日销归零（断货，骤降）→ 6 个部件 × 4 天 = 24 条
--         · part_id % 7  = 0 且 n∈[10,12] → 日销 ×5（促销，激增）→ 9 个部件 × 3 天 = 27 条
-- 幂等：CREATE TABLE IF NOT EXISTS + 先清空再插入（本表由本迁移独占）。
-- 回滚：DROP TABLE sales_daily;
-- ============================================================================
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `sales_daily` (
  `id`                 bigint       NOT NULL AUTO_INCREMENT COMMENT '日销ID',
  `part_id`            bigint       NOT NULL COMMENT '零部件ID（part.id）',
  `warehouse_location` varchar(100) NULL DEFAULT 'A区-1号库' COMMENT '仓库位置（与 inventory 同口径）',
  `sales_date`         date         NOT NULL COMMENT '销售日期',
  `quantity`           int          NOT NULL DEFAULT 0 COMMENT '销售数量',
  `amount`             decimal(14,2) NOT NULL DEFAULT 0.00 COMMENT '销售金额',
  `deleted`            tinyint      NULL DEFAULT 0 COMMENT '删除标志',
  `create_time`        datetime     NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`        datetime     NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_part_wh_date`(`part_id`, `warehouse_location`, `sales_date`) USING BTREE,
  INDEX `idx_sales_date`(`sales_date`) USING BTREE,
  INDEX `idx_part_id`(`part_id`) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = '日销售明细表';

-- 幂等：清空后重建（本表仅由本迁移写入）
DELETE FROM `sales_daily`;

INSERT INTO `sales_daily` (`part_id`, `warehouse_location`, `sales_date`, `quantity`, `amount`)
WITH RECURSIVE `days` (`n`) AS (
    SELECT 0
    UNION ALL
    SELECT `n` + 1 FROM `days` WHERE `n` < 29
),
`base` AS (
    SELECT
        p.`id`                                                  AS part_id,
        COALESCE(i.`warehouse_location`, 'A区-1号库')            AS wh,
        COALESCE(p.`suggested_retail_price`,
                 ROUND(p.`purchase_price` * 1.6, 2), 0)         AS unit_price,
        d.`n`                                                   AS n,
        15 + ((p.`id` * 37 + d.`n` * 17) % 11)                  AS base_qty
    FROM `part` p
    JOIN `inventory` i ON i.`part_id` = p.`id` AND i.`deleted` = 0
    CROSS JOIN `days` d
    WHERE p.`deleted` = 0
),
`calc` AS (
    SELECT
        `part_id`,
        `wh`,
        `unit_price`,
        `n`,
        CASE
            WHEN `part_id` % 11 = 0 AND `n` BETWEEN 3 AND 6   THEN 0
            WHEN `part_id` % 7  = 0 AND `n` BETWEEN 10 AND 12 THEN `base_qty` * 5
            ELSE `base_qty`
        END AS qty
    FROM `base`
)
SELECT
    `part_id`,
    `wh`,
    DATE_SUB(CURDATE(), INTERVAL `n` DAY),
    `qty`,
    ROUND(`qty` * `unit_price`, 2)
FROM `calc`;

INSERT INTO `_migration_log`(`migration_tag`) SELECT 'p2_sales_domain'
WHERE NOT EXISTS (SELECT 1 FROM `_migration_log` WHERE `migration_tag` = 'p2_sales_domain');

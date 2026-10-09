-- ============================================================================
-- P2 语义改名：part → product（2026-09-17） ERP_OPENCLAW
-- 背景：早期演示数据来自摩托车零部件场景，与当前的快消零售供应链语义对不上。
--       本迁移只做【DDL 改名】，不改结构、不动数据内容（主题由 005 换）。
-- 原则：纯改名 —— 表 / 列 / 索引 / 视图一并改成零售语义，不改结构、不改数据。
-- 幂等：每步先用 information_schema 检查再动态执行（沿用 001 的 PREPARE/EXECUTE 模式），
--       重跑自动跳过已完成的部分。
-- 回滚：用迁移前的库备份还原（备份文件不随仓库发布，本地留存）
-- ============================================================================
SET NAMES utf8mb4;

-- ------------------------------------------------------------
-- 0) 表改名
--    part → product / part_supplier → product_supplier / part_price_log → product_price_log
-- ------------------------------------------------------------
SET @need = (SELECT COUNT(*) FROM information_schema.TABLES
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part');
SET @ddl = IF(@need = 1, 'RENAME TABLE `part` TO `product`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.TABLES
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_supplier');
SET @ddl = IF(@need = 1, 'RENAME TABLE `part_supplier` TO `product_supplier`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.TABLES
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_price_log');
SET @ddl = IF(@need = 1, 'RENAME TABLE `part_price_log` TO `product_price_log`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ------------------------------------------------------------
-- 1) 列改名
-- ------------------------------------------------------------
-- product.part_code → product_code
SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'product' AND COLUMN_NAME = 'part_code');
SET @ddl = IF(@need = 1,
    'ALTER TABLE `product` CHANGE `part_code` `product_code` varchar(50) NOT NULL COMMENT ''商品编码''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- inventory.part_id → product_id
SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'inventory' AND COLUMN_NAME = 'part_id');
SET @ddl = IF(@need = 1,
    'ALTER TABLE `inventory` CHANGE `part_id` `product_id` bigint NOT NULL COMMENT ''商品ID''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- order_detail.part_id → product_id
SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'order_detail' AND COLUMN_NAME = 'part_id');
SET @ddl = IF(@need = 1,
    'ALTER TABLE `order_detail` CHANGE `part_id` `product_id` bigint NOT NULL COMMENT ''商品ID''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- product_supplier.part_id → product_id
SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'product_supplier' AND COLUMN_NAME = 'part_id');
SET @ddl = IF(@need = 1,
    'ALTER TABLE `product_supplier` CHANGE `part_id` `product_id` bigint NOT NULL COMMENT ''商品ID''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- product_price_log.part_id / part_name → product_id / product_name
SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'product_price_log' AND COLUMN_NAME = 'part_id');
SET @ddl = IF(@need = 1,
    'ALTER TABLE `product_price_log` CHANGE `part_id` `product_id` bigint NULL COMMENT ''商品ID''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'product_price_log' AND COLUMN_NAME = 'part_name');
SET @ddl = IF(@need = 1,
    'ALTER TABLE `product_price_log` CHANGE `part_name` `product_name` varchar(100) NULL COMMENT ''商品名称''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- sales_daily.part_id → product_id
SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_daily' AND COLUMN_NAME = 'part_id');
SET @ddl = IF(@need = 1,
    'ALTER TABLE `sales_daily` CHANGE `part_id` `product_id` bigint NOT NULL COMMENT ''商品ID（product.id）''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ------------------------------------------------------------
-- 2) 索引改名（列改名不会自动改索引名，留着 part_* 会误导）
-- ------------------------------------------------------------
SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'product' AND INDEX_NAME = 'uk_part_code');
SET @ddl = IF(@need >= 1, 'ALTER TABLE `product` RENAME INDEX `uk_part_code` TO `uk_product_code`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'inventory' AND INDEX_NAME = 'uk_part_id');
SET @ddl = IF(@need >= 1, 'ALTER TABLE `inventory` RENAME INDEX `uk_part_id` TO `uk_product_id`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'order_detail' AND INDEX_NAME = 'idx_part_id');
SET @ddl = IF(@need >= 1, 'ALTER TABLE `order_detail` RENAME INDEX `idx_part_id` TO `idx_product_id`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'product_supplier' AND INDEX_NAME = 'idx_ps_part');
SET @ddl = IF(@need >= 1, 'ALTER TABLE `product_supplier` RENAME INDEX `idx_ps_part` TO `idx_ps_product`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'product_price_log' AND INDEX_NAME = 'idx_ppl_part');
SET @ddl = IF(@need >= 1, 'ALTER TABLE `product_price_log` RENAME INDEX `idx_ppl_part` TO `idx_ppl_product`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_daily' AND INDEX_NAME = 'uk_part_wh_date');
SET @ddl = IF(@need >= 1, 'ALTER TABLE `sales_daily` RENAME INDEX `uk_part_wh_date` TO `uk_product_wh_date`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_daily' AND INDEX_NAME = 'idx_part_id');
SET @ddl = IF(@need >= 1, 'ALTER TABLE `sales_daily` RENAME INDEX `idx_part_id` TO `idx_product_id`', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ------------------------------------------------------------
-- 3) 视图重建（视图列名含 part_*，必须重建；代码未引用，仅为库内自洽）
--    保留原有业务语义：stock_status 三分档阈值不变
-- ------------------------------------------------------------
DROP VIEW IF EXISTS `inventory_warning_view`;
CREATE VIEW `inventory_warning_view` AS
SELECT
    `i`.`id`                   AS `id`,
    `i`.`product_id`           AS `product_id`,
    `p`.`product_code`         AS `product_code`,
    `p`.`name`                 AS `product_name`,
    `i`.`current_quantity`     AS `current_quantity`,
    `i`.`safety_stock`         AS `safety_stock`,
    `p`.`stock_warning_value`  AS `stock_warning_value`,
    `i`.`warehouse_location`   AS `warehouse_location`,
    CASE
        WHEN `i`.`current_quantity` <= `p`.`stock_warning_value` THEN '严重不足'
        WHEN `i`.`current_quantity` <= `i`.`safety_stock`        THEN '不足'
        ELSE '充足'
    END                        AS `stock_status`,
    `i`.`last_inbound_time`    AS `last_inbound_time`,
    `i`.`last_outbound_time`   AS `last_outbound_time`
FROM `inventory` `i`
JOIN `product` `p` ON `i`.`product_id` = `p`.`id`
WHERE `i`.`deleted` = 0 AND `p`.`deleted` = 0;

DROP VIEW IF EXISTS `order_detail_full_view`;
CREATE VIEW `order_detail_full_view` AS
SELECT
    `od`.`id`            AS `detail_id`,
    `od`.`order_id`      AS `order_id`,
    `o`.`order_number`   AS `order_number`,
    `od`.`product_id`    AS `product_id`,
    `p`.`product_code`   AS `product_code`,
    `p`.`name`           AS `product_name`,
    `p`.`model`          AS `model`,
    `p`.`specification`  AS `specification`,
    `od`.`quantity`      AS `quantity`,
    `od`.`unit_price`    AS `unit_price`,
    `od`.`subtotal`      AS `subtotal`,
    `p`.`supplier_id`    AS `supplier_id`,
    `s`.`name`           AS `supplier_name`,
    `s`.`supplier_code`  AS `supplier_code`,
    `od`.`create_time`   AS `detail_create_time`
FROM `order_detail` `od`
JOIN `purchase_order` `o` ON `od`.`order_id` = `o`.`id`
JOIN `product` `p`        ON `od`.`product_id` = `p`.`id`
LEFT JOIN `supplier` `s`  ON `p`.`supplier_id` = `s`.`id`
WHERE `od`.`deleted` = 0 AND `o`.`deleted` = 0 AND `p`.`deleted` = 0;

-- ------------------------------------------------------------
-- 4) 记录
-- ------------------------------------------------------------
INSERT INTO `_migration_log`(`migration_tag`) SELECT 'p2_rename_part_to_product'
WHERE NOT EXISTS (SELECT 1 FROM `_migration_log` WHERE `migration_tag` = 'p2_rename_part_to_product');

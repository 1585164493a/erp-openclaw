-- ============================================================================
-- P2-b 销售域能力补齐：渠道维度 + 销售目标 + 奖金规则（2026-09-18） ERP_OPENCLAW
-- 背景：批次 3 建 sales-analyst 子 Agent 时发现三个缺口：
--   1) sales_daily 只有「区域 DC」维度，没有「销售渠道」维度 → 渠道分析做不了
--   2) 没有销售目标表 → 「奖金解释」缺少基准
--   3) 没有奖金规则表 → 达成率算不出对应奖金档位
-- 本迁移补齐这三样，为 /api/sales/summary 与 /api/sales/bonus 提供数据底座。
-- 幂等：加列/加索引用 information_schema 守卫；两张新表 DROP IF EXISTS 后重建
--       （由本迁移独占）；目标金额按实际值反推，重跑结果一致。
-- 回滚：DROP TABLE sales_target, bonus_rule; ALTER TABLE sales_daily DROP COLUMN channel;
-- ============================================================================
SET NAMES utf8mb4;

-- ------------------------------------------------------------
-- 1) sales_daily 加「销售渠道」维度
-- ------------------------------------------------------------
SET @need = (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_daily'
               AND COLUMN_NAME = 'channel');
SET @ddl = IF(@need = 0,
    'ALTER TABLE `sales_daily` ADD COLUMN `channel` varchar(20) NULL COMMENT ''销售渠道'' AFTER `warehouse_location`',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 回填渠道：按 product_id 轮转，保证三类渠道都有量（与 customer.customer_type 口径一致）
UPDATE `sales_daily`
SET `channel` = ELT(1 + (`product_id` % 3), 'KA商超', '经销商', '电商')
WHERE `channel` IS NULL;

SET @need = (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_daily'
               AND INDEX_NAME = 'idx_channel');
SET @ddl = IF(@need = 0,
    'ALTER TABLE `sales_daily` ADD INDEX `idx_channel`(`channel`)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ------------------------------------------------------------
-- 2) 销售目标表
--    口径：自然月目标（target_month = 'YYYY-MM'），维度内唯一。
--    奖金解释时「实际」取该月 1 日至今，并同时给出时间进度，便于判断是否领先/落后。
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `sales_target`;
CREATE TABLE `sales_target` (
  `id`              bigint       NOT NULL AUTO_INCREMENT COMMENT '目标ID',
  `target_month`    char(7)      NOT NULL COMMENT '目标月份 YYYY-MM',
  `dimension`       varchar(20)  NOT NULL COMMENT '维度：channel / category',
  `dimension_value` varchar(50)  NOT NULL COMMENT '维度取值，如 KA商超 / 洗发护发',
  `target_amount`   decimal(14,2) NOT NULL COMMENT '月度销售目标（元）',
  `owner`           varchar(50)  NULL COMMENT '负责人/团队',
  `remark`          varchar(200) NULL COMMENT '备注',
  `deleted`         tinyint      NULL DEFAULT 0 COMMENT '删除标志',
  `create_time`     datetime     NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`     datetime     NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_month_dim_val`(`target_month`, `dimension`, `dimension_value`) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = '销售目标表';

-- 目标金额 = 全月预计销售额 ÷ 目标达成率。
--   全月预计 = 当月已发生销售额 ÷ 时间进度（DAY(CURDATE())/DAY(LAST_DAY(CURDATE()))）
--   这样「预计达成率」会精确等于下面设定的 rate，便于演示奖金分档：
--     KA商超 → 0.85（基本达标档）  经销商 → 1.05（达成档）  电商 → 0.95（接近达标档）
--   ⚠️ 不能用「当月已发生额 ÷ rate」当目标 —— 那等于按半月定目标，外推会人人超额。
INSERT INTO `sales_target` (`target_month`, `dimension`, `dimension_value`, `target_amount`, `owner`, `remark`)
SELECT DATE_FORMAT(CURDATE(), '%Y-%m'), 'channel', c.`ch`,
       ROUND(COALESCE(a.`amt`, 0)
             / (DAY(CURDATE()) / DAY(LAST_DAY(CURDATE())))
             / c.`rate`, 2),
       c.`owner`, 'P2-b 零售主题重建（整月目标按时间进度外推）'
FROM (
    SELECT 'KA商超' AS `ch`, 0.85 AS `rate`, 'KA渠道组' AS `owner`
    UNION ALL SELECT '经销商', 1.05, '经销渠道组'
    UNION ALL SELECT '电商',   0.95, '电商渠道组'
) c
LEFT JOIN (
    SELECT `channel` AS `ch`, SUM(`amount`) AS `amt`
    FROM `sales_daily`
    WHERE `deleted` = 0
      AND DATE_FORMAT(`sales_date`, '%Y-%m') = DATE_FORMAT(CURDATE(), '%Y-%m')
    GROUP BY `channel`
) a ON a.`ch` = c.`ch`;

-- 品类维度目标（同样按整月外推，预计达成率在 88%~112% 之间分布）
INSERT INTO `sales_target` (`target_month`, `dimension`, `dimension_value`, `target_amount`, `owner`, `remark`)
SELECT DATE_FORMAT(CURDATE(), '%Y-%m'), 'category', a.`category`,
       ROUND(a.`amt`
             / (DAY(CURDATE()) / DAY(LAST_DAY(CURDATE())))
             / (0.88 + (ROW_NUMBER() OVER (ORDER BY a.`category`) % 5) * 0.06), 2),
       '品类组', 'P2-b 零售主题重建（整月目标按时间进度外推）'
FROM (
    SELECT p.`category`, SUM(sd.`amount`) AS `amt`
    FROM `sales_daily` sd JOIN `product` p ON p.`id` = sd.`product_id`
    WHERE sd.`deleted` = 0
      AND DATE_FORMAT(sd.`sales_date`, '%Y-%m') = DATE_FORMAT(CURDATE(), '%Y-%m')
    GROUP BY p.`category`
) a;

-- ------------------------------------------------------------
-- 3) 奖金规则表（达成率区间 → 奖金系数）
--    区间口径：左闭右开 [achieve_min, achieve_max)；最高档 achieve_max 为 NULL 表示无上限。
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `bonus_rule`;
CREATE TABLE `bonus_rule` (
  `id`          bigint      NOT NULL AUTO_INCREMENT COMMENT '规则ID',
  `rule_code`   varchar(20) NOT NULL COMMENT '规则编码',
  `tier_name`   varchar(30) NOT NULL COMMENT '档位名称',
  `achieve_min` decimal(5,2) NOT NULL COMMENT '达成率下限（含）',
  `achieve_max` decimal(5,2) NULL COMMENT '达成率上限（不含），NULL=无上限',
  `bonus_rate`  decimal(5,2) NOT NULL COMMENT '奖金系数',
  `remark`      varchar(200) NULL COMMENT '说明',
  `deleted`     tinyint     NULL DEFAULT 0,
  `create_time` datetime    NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = '奖金规则表';

INSERT INTO `bonus_rule` (`rule_code`, `tier_name`, `achieve_min`, `achieve_max`, `bonus_rate`, `remark`) VALUES
('T1', '未达标',   0.00, 0.70, 0.00, '达成率不足 70%，不计提奖金'),
('T2', '基本达标', 0.70, 0.90, 0.60, '达成 70%~90%，按 0.6 系数计提'),
('T3', '接近达标', 0.90, 1.00, 0.80, '达成 90%~100%，按 0.8 系数计提'),
('T4', '达成',     1.00, 1.20, 1.00, '达成 100%~120%，按 1.0 系数计提'),
('T5', '超额达成', 1.20, NULL, 1.50, '达成 120% 以上，按 1.5 系数计提');

-- ------------------------------------------------------------
-- 4) 记录
-- ------------------------------------------------------------
INSERT INTO `_migration_log`(`migration_tag`) SELECT 'p2b_sales_capability'
WHERE NOT EXISTS (SELECT 1 FROM `_migration_log` WHERE `migration_tag` = 'p2b_sales_capability');

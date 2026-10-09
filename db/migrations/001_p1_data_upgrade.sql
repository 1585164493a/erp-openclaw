-- ============================================================================
-- P1 数据升级（2026-09-08） ERP_OPENCLAW
-- 目标：让采购分析典型问题有真实可分析结果。
-- 原则：只加表/加列/改值，不改既有列语义；执行前已在本地做全量备份。
-- 幂等：关键段以 _migration_log 标记，重跑自动跳过。
-- 回滚：用迁移前的本地全量备份还原（备份文件不随仓库发布）。
-- ============================================================================
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS _migration_log (
    migration_tag VARCHAR(100) PRIMARY KEY,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------
-- 0) 数据质量：供应商名称乱码修复（id=4 含韩文混排）
-- ------------------------------------------------------------
UPDATE supplier
SET name = '昭禾汽车零部件（上海）有限公司', contact_person = '佐藤健一', update_time = NOW()
WHERE id = 4 AND name LIKE '%자동차%';

-- ------------------------------------------------------------
-- 1) purchase_order.supplier_id 落库（缺口 A2）
--    backfill 规则：取该订单明细中小计最大的那条明细所对应 part 的供应商（主导供应商）
-- ------------------------------------------------------------
SET @col_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'purchase_order' AND COLUMN_NAME = 'supplier_id'
);
SET @ddl = IF(@col_exists = 0,
    'ALTER TABLE purchase_order ADD COLUMN supplier_id BIGINT NULL COMMENT ''下单主导供应商（按明细最大小计归集）'' AFTER created_by',
    'SELECT 1');
PREPARE s1 FROM @ddl; EXECUTE s1; DEALLOCATE PREPARE s1;

UPDATE purchase_order po
LEFT JOIN (
    SELECT od.order_id, p.supplier_id,
           ROW_NUMBER() OVER (PARTITION BY od.order_id ORDER BY od.subtotal DESC, od.id) AS rn
    FROM order_detail od
    JOIN part p ON p.id = od.part_id
    WHERE od.deleted = 0
) t ON t.order_id = po.id AND t.rn = 1
SET po.supplier_id = t.supplier_id, po.update_time = NOW()
WHERE po.supplier_id IS NULL;

INSERT INTO _migration_log(migration_tag) SELECT 'p1_supplier_id'
WHERE NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_supplier_id');

-- ------------------------------------------------------------
-- 2) 数据质量：订单编号统一为 PO+下单日期-4位序号（缺口 B3）
--    新规则与工具自动生成格式（PO+yyyymmdd+3随机）不冲突，且与 order_time 年份一致
-- ------------------------------------------------------------
UPDATE purchase_order
SET order_number = CONCAT('PO', DATE_FORMAT(order_time, '%Y%m%d'), '-', LPAD(id, 4, '0')),
    update_time = NOW();

-- ------------------------------------------------------------
-- 3) 数据质量：删除孤儿订单明细（part_id=164 在 part 表不存在，缺口 B2）
-- ------------------------------------------------------------
DELETE od FROM order_detail od
LEFT JOIN part p ON p.id = od.part_id
WHERE p.id IS NULL AND od.deleted = 0;

-- ------------------------------------------------------------
-- 4) 价格历史真实化（缺口 A3）：部分物料 2024-2026 按时间呈现价格阶梯
--    生成列 subtotal 自动重算；随后统一重算 PO 总额保证对账
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS part_price_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    part_id BIGINT NOT NULL,
    part_name VARCHAR(100) NULL,
    note VARCHAR(200) NULL,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_ppl_part (part_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

UPDATE order_detail od
JOIN purchase_order po ON po.id = od.order_id
SET od.unit_price = CASE
        WHEN od.part_id = 25 THEN  -- 火花塞 C7HSA 78 -> 68/74/78（阶梯上涨）
            CASE WHEN po.order_time < '2025-07-01' THEN 68
                 WHEN po.order_time < '2026-02-01' THEN 74
                 ELSE 78 END
        WHEN od.part_id = 36 THEN  -- 后刹车片 249 -> 232/245/249
            CASE WHEN po.order_time < '2025-01-01' THEN 232
                 WHEN po.order_time < '2026-01-01' THEN 245
                 ELSE 249 END
        WHEN od.part_id = 1 THEN   -- 活塞环组件 3905 -> 3650/3800/3905
            CASE WHEN po.order_time < '2025-07-01' THEN 3650
                 WHEN po.order_time < '2026-01-01' THEN 3800
                 ELSE 3905 END
        WHEN od.part_id = 10 THEN  -- 离合器片 2877 -> 2700/2850/2877
            CASE WHEN po.order_time < '2025-01-01' THEN 2700
                 WHEN po.order_time < '2026-01-01' THEN 2850
                 ELSE 2877 END
        WHEN od.part_id = 42 THEN  -- 离合线 72 -> 68/71/72
            CASE WHEN po.order_time < '2025-01-01' THEN 68
                 WHEN po.order_time < '2026-01-01' THEN 71
                 ELSE 72 END
        WHEN od.part_id = 52 THEN  -- 链条接头 1816 -> 1700/1790/1816
            CASE WHEN po.order_time < '2025-01-01' THEN 1700
                 WHEN po.order_time < '2026-01-01' THEN 1790
                 ELSE 1816 END
        WHEN od.part_id = 3 THEN   -- 曲轴总成 4744 -> 4520/4680/4744
            CASE WHEN po.order_time < '2025-01-01' THEN 4520
                 WHEN po.order_time < '2026-01-01' THEN 4680
                 ELSE 4744 END
        WHEN od.part_id = 60 THEN  -- 油箱盖 14 -> 17/15/14（谈判降价，可讲“成本优化”故事）
            CASE WHEN po.order_time < '2025-01-01' THEN 17
                 WHEN po.order_time < '2026-01-01' THEN 15
                 ELSE 14 END
        ELSE od.unit_price
    END
WHERE od.deleted = 0
  AND od.part_id IN (25, 36, 1, 10, 42, 52, 3, 60)
  AND NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_price');

INSERT INTO part_price_log (part_id, part_name, note)
SELECT id, name, 'P1 价格阶梯：24-26 年历史订单单价按时间调整' FROM part WHERE id IN (25,36,1,10,42,52,3,60);

-- 统一重算全部订单总额（保证 总金额 = SUM(明细 subtotal)）
UPDATE purchase_order po
SET po.total_amount = COALESCE((
        SELECT ROUND(SUM(od.subtotal), 2) FROM order_detail od
        WHERE od.order_id = po.id AND od.deleted = 0
    ), 0),
    po.update_time = NOW();

INSERT INTO _migration_log(migration_tag) SELECT 'p1_price'
WHERE NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_price');

-- ------------------------------------------------------------
-- 5) 交期真实化（缺口 A5）：已入库订单(85)的实际到货日相对预计日小幅波动(-2~+2 天)
-- ------------------------------------------------------------
UPDATE purchase_order
SET actual_delivery_date = DATE_ADD(expected_delivery_date,
        INTERVAL ELT(MOD(id, 8) + 1, 0, 0, -1, 1, 0, 2, 0, -2) DAY),
    update_time = NOW()
WHERE status = 4
  AND actual_delivery_date IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_delivery');

INSERT INTO _migration_log(migration_tag) SELECT 'p1_delivery'
WHERE NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_delivery');

-- ------------------------------------------------------------
-- 6) 候选供应商/报价表（缺口 A1）：同物料多供应商报价，供“比价/备选供应商”分析
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS part_supplier (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    part_id BIGINT NOT NULL COMMENT '零部件ID',
    supplier_id BIGINT NOT NULL COMMENT '供应商ID',
    quote_price DECIMAL(10,2) NOT NULL COMMENT '报价单价',
    is_primary TINYINT NOT NULL DEFAULT 0 COMMENT '是否当前主供应商(1=是,0=候选)',
    lead_days INT NULL COMMENT '交期(天)',
    moq INT NULL COMMENT '最小起订量',
    valid_from DATE NOT NULL,
    valid_to DATE NULL,
    remark VARCHAR(200) NULL,
    deleted TINYINT NOT NULL DEFAULT 0,
    create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_ps_part (part_id),
    KEY idx_ps_supplier (supplier_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='零部件-供应商报价表（主+候选）';

INSERT INTO part_supplier (part_id, supplier_id, quote_price, is_primary, lead_days, moq, valid_from, valid_to, remark)
SELECT t.part_id, t.supplier_id, t.quote_price, t.is_primary, t.lead_days, t.moq, t.valid_from, t.valid_to, t.remark
FROM (
    SELECT 65 AS part_id, 31 AS supplier_id, 32.00 AS quote_price, 1 AS is_primary, 7 AS lead_days, 20 AS moq, '2024-01-01' AS valid_from, NULL AS valid_to, '主供应商' AS remark
    UNION ALL SELECT 65, 32, 33.50, 0, 5, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 65, 33, 31.00, 0, 6, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 65, 1, 34.00, 0, 8, 30, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 66, 32, 40.00, 1, 5, 20, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 66, 1, 39.00, 0, 8, 30, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 66, 33, 41.50, 0, 6, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 66, 31, 40.80, 0, 7, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 67, 33, 58.00, 1, 6, 20, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 67, 1, 56.50, 0, 8, 30, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 67, 32, 59.50, 0, 5, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 67, 31, 57.00, 0, 7, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 25, 1, 78.00, 1, 8, 20, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 25, 31, 76.00, 0, 7, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 25, 32, 77.50, 0, 5, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 25, 33, 75.00, 0, 6, 50, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 36, 1, 249.00, 1, 8, 10, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 36, 3, 240.00, 0, 12, 20, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 36, 22, 245.50, 0, 10, 20, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 35, 22, 59.00, 1, 10, 20, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 35, 1, 57.00, 0, 8, 30, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 35, 3, 61.00, 0, 12, 20, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 1, 26, 3905.00, 1, 14, 5, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 1, 27, 3820.00, 0, 12, 10, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 1, 5, 3950.00, 0, 15, 10, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 42, 15, 72.00, 1, 7, 50, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 42, 23, 70.50, 0, 6, 100, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 42, 9, 73.80, 0, 8, 100, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 60, 19, 14.00, 1, 5, 200, '2024-01-01', NULL, '主供应商'
    UNION ALL SELECT 60, 12, 13.20, 0, 5, 300, '2026-01-01', NULL, 'P1 候选报价'
    UNION ALL SELECT 60, 16, 14.80, 0, 6, 300, '2026-01-01', NULL, 'P1 候选报价'
) t
WHERE NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_part_supplier')
  AND EXISTS (SELECT 1 FROM part p WHERE p.id = t.part_id)
  AND EXISTS (SELECT 1 FROM supplier s WHERE s.id = t.supplier_id);

INSERT INTO _migration_log(migration_tag) SELECT 'p1_part_supplier'
WHERE NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_part_supplier');
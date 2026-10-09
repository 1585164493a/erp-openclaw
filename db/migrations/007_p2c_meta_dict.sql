-- ============================================================================
-- P2-c 元数据字典：补齐「枚举字段的业务含义」（2026-09-18） ERP_OPENCLAW
-- 背景：主数据子 Agent 要回答"customer_type=1 是什么"这类问题，但数据库里
--       只存了数字编码，列注释也只写"客户类型"——不含取值含义。
--       模型拿不到就只能自己编，属于启动期校验拦不住的软错误。
-- 设计：只建「业务语义字典」这一层。表/字段结构**不抄进来** ——
--       结构由 /api/meta/schema 实时读 information_schema，
--       避免"改了列名但字典表没同步"的不一致。
--       能自动获取的绝不手工维护，只能人工写的才建表。
-- 幂等：DROP IF EXISTS 后重建（本表由本迁移独占）。
-- 回滚：DROP TABLE meta_dict;
-- ============================================================================
SET NAMES utf8mb4;

DROP TABLE IF EXISTS `meta_dict`;
CREATE TABLE `meta_dict` (
  `id`          bigint       NOT NULL AUTO_INCREMENT COMMENT '字典ID',
  `dict_type`   varchar(16)  NOT NULL COMMENT '字典类型：table=表用途 / enum=字段取值语义',
  `table_name`  varchar(50)  NOT NULL COMMENT '表名',
  `column_name` varchar(50)  NULL COMMENT '字段名（dict_type=enum 时必填）',
  `value_code`  varchar(50)  NULL COMMENT '编码值（dict_type=enum 时必填）',
  `meaning`     varchar(200) NOT NULL COMMENT '业务含义',
  `sort_no`     int          NULL DEFAULT 0 COMMENT '展示顺序',
  `deleted`     tinyint      NULL DEFAULT 0 COMMENT '删除标志',
  `create_time` datetime     NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime     NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_dict`(`dict_type`, `table_name`, `column_name`, `value_code`) USING BTREE,
  INDEX `idx_table`(`table_name`, `column_name`) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = '元数据业务字典';

-- ------------------------------------------------------------
-- 1) 表用途（回答"这个数据在哪张表"）
-- ------------------------------------------------------------
INSERT INTO `meta_dict` (`dict_type`,`table_name`,`column_name`,`value_code`,`meaning`,`sort_no`) VALUES
('table','product',NULL,NULL,'商品主数据：华联日化 SKU（品牌名、系列、包装规格、计量单位、采购价、建议零售价、品类、安全库存阈值）',1),
('table','product_supplier',NULL,NULL,'商品-供应商报价：多供应商比价用（报价、交期天数、最小起订量、是否当前主供应商、有效期）',2),
('table','product_price_log',NULL,NULL,'商品调价记录：记录商品价格调整的时间与事由',3),
('table','supplier',NULL,NULL,'供应商主数据：原料供应商 / 包材供应商 / 代工厂 / 自有工厂，含信用评级与合作状态',4),
('table','customer',NULL,NULL,'客户主数据：KA 商超 / 区域经销商 / 电商平台，含客户类型与折扣等级',5),
('table','inventory',NULL,NULL,'库存：按商品维护当前库存、安全库存与所在区域 DC（一个商品一条记录）',6),
('table','purchase_order',NULL,NULL,'采购订单主表：单号、总额、状态、下单时间、预计/实际交货日期、主导供应商',7),
('table','order_detail',NULL,NULL,'采购订单明细：订单下的商品、数量、单价与小计（subtotal 为数据库生成列）',8),
('table','logistics',NULL,NULL,'物流运单：承运商、运单号、发货时间、预计/实际到达时间、物流状态（一张订单可有多条）',9),
('table','sales_daily',NULL,NULL,'日销售明细：商品 × 销售渠道 × 仓库位置 × 日期 的销量与销售额（异常分析与奖金达成的数据源）',10),
('table','sales_target',NULL,NULL,'销售目标：按月份 + 维度（渠道/品类）的月度销售目标与负责人',11),
('table','bonus_rule',NULL,NULL,'奖金规则：达成率区间 → 奖金系数（左闭右开区间，最高档上限为 NULL）',12),
('table','user',NULL,NULL,'系统用户：账号、角色、所属部门',13);

-- ------------------------------------------------------------
-- 2) 字段取值语义（回答"这个数字是什么意思"）
--    ⚠️ 只放「编码型」字段。channel / product.category 这类「值即含义」的字段
--       不在此维护 —— /api/meta/dict 会回退为动态读取 DISTINCT 值。
-- ------------------------------------------------------------
INSERT INTO `meta_dict` (`dict_type`,`table_name`,`column_name`,`value_code`,`meaning`,`sort_no`) VALUES
-- 客户类型
('enum','customer','customer_type','1','KA商超（大型连锁商超渠道）',1),
('enum','customer','customer_type','2','经销商（区域分销商）',2),
('enum','customer','customer_type','3','电商（线上销售平台）',3),
-- 折扣等级（数字越小，折扣力度越大）
('enum','customer','discount_level','1','一级客户（折扣力度最大）',1),
('enum','customer','discount_level','2','二级客户（折扣力度中等）',2),
('enum','customer','discount_level','3','三级客户（折扣力度最小）',3),
-- 供应商信用评级
('enum','supplier','credit_rating','A','优秀（交付准时、质量稳定）',1),
('enum','supplier','credit_rating','B','良好（偶有波动）',2),
('enum','supplier','credit_rating','C','一般（需持续关注）',3),
('enum','supplier','credit_rating','D','较差（审慎合作）',4),
-- 供应商合作状态
('enum','supplier','status','1','合作中',1),
('enum','supplier','status','0','已停用',2),
-- 采购订单状态
('enum','purchase_order','status','1','待审核',1),
('enum','purchase_order','status','2','已审核',2),
('enum','purchase_order','status','3','采购中',3),
('enum','purchase_order','status','4','已入库',4),
('enum','purchase_order','status','5','已取消',5),
-- 物流状态
('enum','logistics','status','1','待发货',1),
('enum','logistics','status','2','运输中',2),
('enum','logistics','status','3','已签收',3),
('enum','logistics','status','4','异常',4),
-- 是否主供应商
('enum','product_supplier','is_primary','1','当前主供应商',1),
('enum','product_supplier','is_primary','0','候选（备选）供应商',2),
-- 通用软删除标志
('enum','product','deleted','0','有效',1),
('enum','product','deleted','1','已删除（查询时需过滤 deleted=0）',2);

-- ------------------------------------------------------------
-- 3) 记录
-- ------------------------------------------------------------
INSERT INTO `_migration_log`(`migration_tag`) SELECT 'p2c_meta_dict'
WHERE NOT EXISTS (SELECT 1 FROM `_migration_log` WHERE `migration_tag` = 'p2c_meta_dict');

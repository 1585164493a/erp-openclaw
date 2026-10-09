-- ============================================================================
-- P1 数据升级-清理测试残留订单（2026-09-08） ERP_OPENCLAW
-- 背景：motorparts_db.sql 种子内含 4 张 agent/mcp 测试单（id 66/121/122/123），
--       deleted 为 NULL、remark 注明测试、明细价格捏造且 deleted=NULL，
--       导致 supplier_id 归集覆盖率不足 100%、且含孤儿明细(part 164)。
-- 处理：物理删除 4 张测试单及其明细、物流记录（操作前已在本地做完整备份）。
-- 幂等：仅删 deleted IS NULL 的测试残留，真实订单 deleted=0 不受影响。
-- ============================================================================
SET NAMES utf8mb4;

DELETE lg FROM logistics lg
JOIN purchase_order po ON po.id = lg.order_id
WHERE po.id IN (66,121,122,123) AND po.deleted IS NULL;

DELETE od FROM order_detail od
JOIN purchase_order po ON po.id = od.order_id
WHERE po.id IN (66,121,122,123) AND po.deleted IS NULL;

DELETE FROM purchase_order WHERE id IN (66,121,122,123) AND deleted IS NULL;

INSERT INTO _migration_log(migration_tag) SELECT 'p1_cleanup_test_orders'
WHERE NOT EXISTS (SELECT 1 FROM _migration_log WHERE migration_tag = 'p1_cleanup_test_orders');
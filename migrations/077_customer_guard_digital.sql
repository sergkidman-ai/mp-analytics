-- поток: fin. Account isolation remains mandatory; activation is a runtime gate.
BEGIN;
ALTER TABLE customer_guard_orders DROP CONSTRAINT IF EXISTS customer_guard_orders_supported_cabinet;
ALTER TABLE customer_guard_orders ADD CONSTRAINT customer_guard_orders_supported_cabinet
 CHECK ((platform='ozon' AND account IN ('oz_acc1','oz_acc2'))
 OR (platform='wb' AND account IN ('wb_acc1','wb_acc2'))
 OR (platform='yandex' AND account='ya_acc1'));
COMMIT;

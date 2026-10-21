-- поток: fin. Expand only the customer-order pilot; activation remains explicit.
BEGIN;
ALTER TABLE customer_guard_orders DROP CONSTRAINT IF EXISTS customer_guard_orders_platform_check;
ALTER TABLE customer_guard_orders DROP CONSTRAINT IF EXISTS customer_guard_orders_account_check;
ALTER TABLE customer_guard_orders DROP CONSTRAINT IF EXISTS customer_guard_orders_supported_cabinet;
ALTER TABLE customer_guard_orders ADD CONSTRAINT customer_guard_orders_supported_cabinet
 CHECK ((platform='ozon' AND account='oz_acc2') OR (platform='wb' AND account='wb_acc2'));
COMMIT;

-- Increase the missing-MoySklad grace period to 35 minutes for existing and new orders.
BEGIN;
ALTER TABLE customer_guard_orders DROP CONSTRAINT customer_guard_orders_check;
UPDATE customer_guard_orders
SET ms_due_at=first_seen_at+interval '35 minutes';
ALTER TABLE customer_guard_orders
  ADD CONSTRAINT customer_guard_orders_check
  CHECK(ms_due_at=first_seen_at+interval '35 minutes');
COMMIT;

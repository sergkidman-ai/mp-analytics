-- поток: inv. Status observations are independent from financial checks.
BEGIN;
ALTER TABLE customer_guard_orders ADD COLUMN IF NOT EXISTS ms_status_seen_at timestamptz;
COMMENT ON COLUMN customer_guard_orders.ms_status_seen_at IS
  'Time of a verified MS status read; does not imply a new composition, cost or floor calculation';
COMMIT;

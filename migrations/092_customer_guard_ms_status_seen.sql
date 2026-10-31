-- поток: fin. A status-only MS read must not change the timestamp of a cost check.
ALTER TABLE customer_guard_orders
  ADD COLUMN IF NOT EXISTS ms_status_seen_at timestamptz;

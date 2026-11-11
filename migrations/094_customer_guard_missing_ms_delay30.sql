-- поток: inv
BEGIN;
-- поток: inv. Run once while customer-order-guard is stopped; no notifications are sent here.
-- Replace the database-enforced former 70-minute policy for every row.
ALTER TABLE customer_guard_orders DROP CONSTRAINT customer_guard_orders_check;
CREATE TEMP TABLE changed_missing_due ON COMMIT DROP AS
SELECT id, first_seen_at + interval '30 minutes' AS new_due
FROM customer_guard_orders
WHERE ms_due_at = first_seen_at + interval '70 minutes';

UPDATE customer_guard_orders o SET ms_due_at = c.new_due
FROM changed_missing_due c WHERE o.id = c.id;

ALTER TABLE customer_guard_orders ADD CONSTRAINT customer_guard_orders_check
CHECK(ms_due_at = first_seen_at + interval '30 minutes');

UPDATE customer_guard_runtime
SET settings=jsonb_set(settings, '{initial_delay_minutes}', '30'::jsonb);

UPDATE customer_guard_jobs j SET due_at = LEAST(j.due_at, c.new_due)
FROM changed_missing_due c, customer_guard_orders o
WHERE j.order_id = c.id AND o.id = c.id AND o.ms_checked_at IS NULL
  AND o.mp_status IN ('awaiting_packaging', 'awaiting_deliver', 'delivering')
  AND j.state IN ('pending', 'retry');

-- A previous missing check can become eligible sooner. Recheck its facts before delivery;
-- never reopen already sent/suppressed/uncertain notifications or replace active leases.
UPDATE customer_guard_jobs j
SET state='pending', kind='initial_ms_check', due_at=GREATEST(c.new_due, now()),
    attempts=0, error=NULL, lease_token=NULL, lease_until=NULL
FROM changed_missing_due c, customer_guard_orders o
WHERE j.order_id=c.id AND o.id=c.id AND j.state='done' AND o.ms_id IS NULL
  AND o.mp_status IN ('awaiting_packaging', 'awaiting_deliver', 'delivering')
  AND EXISTS (
    SELECT 1 FROM customer_guard_incidents i
    JOIN customer_guard_checks check_row ON check_row.id=o.latest_check_id
    WHERE i.order_id=o.id AND i.kind='missing_ms' AND i.delivery='pending'
      AND i.resolved_at IS NULL AND i.check_id=check_row.id
      AND check_row.generation=o.generation AND check_row.result->>'signal'='missing_ms'
  );

COMMIT;

-- Customer guard: 70-minute grace period after first observation.
BEGIN;
ALTER TABLE customer_guard_orders DROP CONSTRAINT customer_guard_orders_check;
UPDATE customer_guard_orders SET ms_due_at=first_seen_at+interval '70 minutes';
ALTER TABLE customer_guard_orders ADD CONSTRAINT customer_guard_orders_check
  CHECK(ms_due_at=first_seen_at+interval '70 minutes');
UPDATE customer_guard_runtime
SET settings=jsonb_set(settings,'{initial_delay_minutes}','70'::jsonb);
-- Preserve retry backoffs and leases while postponing checks to the new deadline.
UPDATE customer_guard_jobs j SET due_at=greatest(j.due_at,o.ms_due_at)
FROM customer_guard_orders o WHERE j.order_id=o.id
AND j.state IN ('pending','retry','running') AND j.due_at<o.ms_due_at;
-- A missing check completed under the old grace period needs fresh confirmation at 70m.
UPDATE customer_guard_jobs j
SET state='pending',due_at=o.ms_due_at,attempts=0,lease_token=NULL,lease_until=NULL,error=NULL
FROM customer_guard_orders o JOIN customer_guard_checks c ON c.id=o.latest_check_id
WHERE j.order_id=o.id AND j.state='done' AND o.ms_id IS NULL
AND o.ms_due_at>now() AND c.generation=o.generation
AND c.result->>'signal'='missing_ms'
AND o.mp_status IN ('awaiting_packaging','awaiting_deliver','delivering');
COMMIT;

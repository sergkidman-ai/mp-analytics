-- поток: fin. Activation is explicit; importing code never enables processing.
BEGIN;
CREATE TABLE IF NOT EXISTS customer_guard_runtime (
 id boolean PRIMARY KEY DEFAULT true CHECK(id),
 enabled boolean NOT NULL DEFAULT false,
 notifications_enabled boolean NOT NULL DEFAULT false,
 activated_at timestamptz,
 heartbeat_at timestamptz,
 last_tick_at timestamptz,
 last_error text,
 settings jsonb NOT NULL DEFAULT '{"new_orders_minutes":10,"initial_delay_minutes":10,"repeat_minutes":60,"backfill_days":90}'::jsonb
);
INSERT INTO customer_guard_runtime(id) VALUES(true) ON CONFLICT DO NOTHING;
ALTER TABLE customer_guard_incidents ADD COLUMN IF NOT EXISTS delivery_error text;
COMMIT;

-- поток: fin. Additive pilot schema; no schedules or notification activation.
BEGIN;
CREATE TABLE IF NOT EXISTS customer_guard_orders (
 id bigserial PRIMARY KEY,
 platform text NOT NULL DEFAULT 'ozon' CHECK(platform='ozon'),
 account text NOT NULL DEFAULT 'oz_acc2' CHECK(account='oz_acc2'),
 posting_number text NOT NULL, parent_number text,
 first_seen_at timestamptz NOT NULL, observed_at timestamptz NOT NULL,
 ms_due_at timestamptz NOT NULL, generation bigint NOT NULL DEFAULT 1,
 mp_status text NOT NULL, mp_hash text NOT NULL, mp_payload jsonb NOT NULL,
 ms_id uuid, ms_status text, ms_store text, ms_checked_at timestamptz,
 latest_check_id bigint,
 UNIQUE(platform,account,posting_number),
 CHECK(ms_due_at=first_seen_at+interval '10 minutes')
);
CREATE TABLE IF NOT EXISTS customer_guard_snapshots (
 id bigserial PRIMARY KEY, order_id bigint NOT NULL REFERENCES customer_guard_orders(id),
 source text NOT NULL, api_version text NOT NULL,
 fetched_at timestamptz NOT NULL, hash text NOT NULL, payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS customer_guard_snapshots_order ON customer_guard_snapshots(order_id,id DESC);
CREATE TABLE IF NOT EXISTS customer_guard_checks (
 id bigserial PRIMARY KEY, order_id bigint NOT NULL REFERENCES customer_guard_orders(id),
 generation bigint NOT NULL, calculated_at timestamptz NOT NULL,
 result jsonb NOT NULL, input_hash text NOT NULL
);
CREATE INDEX IF NOT EXISTS customer_guard_checks_order ON customer_guard_checks(order_id,id DESC);
CREATE TABLE IF NOT EXISTS customer_guard_ms_links (
 order_id bigint NOT NULL REFERENCES customer_guard_orders(id),
 entity_type text NOT NULL CHECK(entity_type IN ('customerorder','demand')),
 entity_id uuid NOT NULL, checked_at timestamptz NOT NULL, version text,
 PRIMARY KEY(order_id,entity_type,entity_id)
);
CREATE TABLE IF NOT EXISTS customer_guard_cost_sources (
 id bigserial PRIMARY KEY, order_id bigint NOT NULL REFERENCES customer_guard_orders(id),
 check_id bigint NOT NULL REFERENCES customer_guard_checks(id),
 source jsonb NOT NULL, observed_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_guard_jobs (
 order_id bigint PRIMARY KEY REFERENCES customer_guard_orders(id),
 generation bigint NOT NULL, kind text NOT NULL DEFAULT 'initial_ms_check',
 due_at timestamptz NOT NULL, state text NOT NULL DEFAULT 'pending',
 attempts int NOT NULL DEFAULT 0, lease_token uuid, lease_until timestamptz,
 error text, CHECK(state IN ('pending','running','retry','done','error'))
);
CREATE INDEX IF NOT EXISTS customer_guard_jobs_due ON customer_guard_jobs(due_at)
 WHERE state IN ('pending','retry','running');
CREATE TABLE IF NOT EXISTS customer_guard_incidents (
 id bigserial PRIMARY KEY, order_id bigint NOT NULL REFERENCES customer_guard_orders(id),
 kind text NOT NULL, dedup_key text NOT NULL UNIQUE, opened_at timestamptz NOT NULL,
 resolved_at timestamptz, check_id bigint NOT NULL REFERENCES customer_guard_checks(id),
 delivery text NOT NULL DEFAULT 'pending',
 recipient text, telegram_message_id bigint, attempted_at timestamptz,
 CHECK(delivery IN ('pending','sending','sent','uncertain','failed','suppressed'))
);
CREATE TABLE IF NOT EXISTS customer_guard_sync_state (
 scope text PRIMARY KEY, started_at timestamptz, completed_at timestamptz,
 window_from timestamptz, window_to timestamptz,
 row_count int, error text, details jsonb NOT NULL DEFAULT '{}'
);
COMMIT;

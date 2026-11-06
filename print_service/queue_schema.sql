-- поток: print. Install explicitly inside a dedicated schema, never public.
CREATE TABLE IF NOT EXISTS snapshots (
    id uuid PRIMARY KEY,
    day date NOT NULL,
    started_at timestamptz NOT NULL,
    raw_orders jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS current_snapshot (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    snapshot_id uuid REFERENCES snapshots(id)
);
INSERT INTO current_snapshot(singleton) VALUES (true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS orders (
    id uuid PRIMARY KEY,
    account text NOT NULL CHECK (account IN ('oz_acc1','oz_acc2','wb_acc1','wb_acc2','ya_acc1')),
    external_id text NOT NULL,
    demand_id uuid,
    counter_key text NOT NULL,
    snapshot_id uuid NOT NULL REFERENCES snapshots(id),
    source_error text,
    display jsonb NOT NULL DEFAULT '{}',
    UNIQUE(account, external_id)
);
CREATE TABLE IF NOT EXISTS batches (
    id uuid PRIMARY KEY,
    counter_key text NOT NULL,
    day date NOT NULL,
    state text NOT NULL CHECK (state IN ('building','ready','failed')),
    generation integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    pair_dir text,
    manifest jsonb,
    error text
);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS display jsonb NOT NULL DEFAULT '{}';
ALTER TABLE batches ADD COLUMN IF NOT EXISTS display jsonb NOT NULL DEFAULT '{}';
CREATE UNIQUE INDEX IF NOT EXISTS one_active_counter
    ON batches(counter_key, day) WHERE state='building';
CREATE TABLE IF NOT EXISTS members (
    batch_id uuid NOT NULL REFERENCES batches(id),
    order_id uuid NOT NULL REFERENCES orders(id),
    ordinal integer NOT NULL,
    identity jsonb NOT NULL,
    PRIMARY KEY(batch_id, order_id),
    UNIQUE(batch_id, ordinal)
);
-- Claims survive successful publication; a printed order cannot re-enter a batch.
CREATE TABLE IF NOT EXISTS claims (
    order_id uuid PRIMARY KEY REFERENCES orders(id),
    batch_id uuid NOT NULL REFERENCES batches(id)
);
CREATE TABLE IF NOT EXISTS requests (
    request_key text PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES batches(id)
);
-- Explicit user confirmation of paper printed outside this page. No fake PDF batch.
CREATE TABLE IF NOT EXISTS external_prints (
    order_id uuid PRIMARY KEY REFERENCES orders(id),
    day date NOT NULL,
    recorded_at timestamptz NOT NULL,
    evidence jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    order_id uuid PRIMARY KEY REFERENCES orders(id),
    demand_id uuid NOT NULL,
    source_key text NOT NULL DEFAULT '',
    receipt_path text,
    receipt_sha text,
    label_path text,
    label_sha text
);
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS source_key text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS sync_runs (
    id uuid PRIMARY KEY,
    state text NOT NULL CHECK(state IN ('pending','running','ready','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    error text
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_sync ON sync_runs((true)) WHERE state IN ('pending','running');
CREATE TABLE IF NOT EXISTS worker_status (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    last_seen timestamptz NOT NULL
);

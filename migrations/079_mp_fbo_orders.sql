-- поток: fin. FBO marketplace orders are deliberately separate from MS-check jobs.
CREATE TABLE IF NOT EXISTS mp_fbo_orders (
  id bigserial PRIMARY KEY,
  platform text NOT NULL CHECK (platform IN ('ozon','wb')),
  account text NOT NULL,
  posting_number text NOT NULL,
  parent_number text,
  created_at timestamptz NOT NULL,
  status_at timestamptz,
  observed_at timestamptz NOT NULL,
  mp_status text NOT NULL,
  source_status text NOT NULL,
  warehouse text,
  products jsonb NOT NULL,
  mp_price numeric,
  price_source text,
  source_api text NOT NULL,
  source_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  input_hash text NOT NULL,
  UNIQUE(platform,account,posting_number),
  CHECK (jsonb_typeof(products)='array'),
  CHECK (mp_price IS NULL OR mp_price>=0)
);
CREATE INDEX IF NOT EXISTS mp_fbo_orders_recent_idx ON mp_fbo_orders(created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS mp_fbo_orders_account_idx ON mp_fbo_orders(account,created_at DESC);

CREATE TABLE IF NOT EXISTS mp_fbo_order_events (
  id bigserial PRIMARY KEY,
  order_id bigint NOT NULL REFERENCES mp_fbo_orders(id),
  observed_at timestamptz NOT NULL,
  status_at timestamptz,
  mp_status text NOT NULL,
  source_status text NOT NULL,
  warehouse text,
  products jsonb NOT NULL,
  mp_price numeric,
  source_api text NOT NULL,
  source_evidence jsonb NOT NULL,
  input_hash text NOT NULL,
  UNIQUE(order_id,input_hash)
);

CREATE TABLE IF NOT EXISTS mp_fbo_sync_state (
  scope text PRIMARY KEY,
  started_at timestamptz NOT NULL,
  completed_at timestamptz NOT NULL,
  window_from timestamptz,
  window_to timestamptz,
  source_rows integer NOT NULL,
  saved_rows integer NOT NULL,
  skipped_rows integer NOT NULL DEFAULT 0
);

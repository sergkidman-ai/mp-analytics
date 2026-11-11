-- поток: fin. Restore FBO storage and empty link schemas; no historical data-specific DML.
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

-- поток: fin. A stock-delta/order match is a candidate, not a traced WB unit or COGS proof.
CREATE TABLE IF NOT EXISTS mp_fbo_stock_link_candidates (
  id bigserial PRIMARY KEY,
  account text NOT NULL,
  order_id bigint NOT NULL REFERENCES mp_fbo_orders(id),
  sku text NOT NULL,
  source_warehouse text NOT NULL,
  previous_capture date NOT NULL,
  current_capture date NOT NULL,
  quantity numeric NOT NULL CHECK (quantity > 0),
  evidence jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (previous_capture < current_capture),
  UNIQUE (account, sku, source_warehouse, previous_capture, current_capture),
  UNIQUE (order_id, sku, previous_capture, current_capture)
);

-- поток: fin. A cross-API stock/order match is an inference, not a WB movement ID or COGS proof.
CREATE TABLE IF NOT EXISTS mp_fbo_cross_source_link_candidates (
  id bigserial PRIMARY KEY,
  account text NOT NULL,
  order_id bigint NOT NULL REFERENCES mp_fbo_orders(id),
  sku text NOT NULL,
  source_warehouse text NOT NULL,
  previous_capture date NOT NULL,
  current_capture_id bigint NOT NULL REFERENCES wb_fbo_intraday_capture(id),
  quantity numeric NOT NULL CHECK (quantity > 0),
  evidence jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_id,sku,previous_capture,current_capture_id)
);

CREATE INDEX IF NOT EXISTS mp_fbo_cross_source_link_lookup
  ON mp_fbo_cross_source_link_candidates(account,sku,source_warehouse);

-- поток: fin. Inferred WB order/stock movement matches; never a movement ID or COGS proof.
CREATE TABLE IF NOT EXISTS mp_fbo_intraday_link_candidates (
  id bigserial PRIMARY KEY,
  account text NOT NULL CHECK (account='wb_acc1'),
  order_id bigint NOT NULL REFERENCES mp_fbo_orders(id),
  sku text NOT NULL,
  source_warehouse text NOT NULL,
  previous_capture_id bigint NOT NULL REFERENCES wb_fbo_intraday_capture(id),
  current_capture_id bigint NOT NULL REFERENCES wb_fbo_intraday_capture(id),
  quantity numeric NOT NULL CHECK (quantity=1),
  evidence jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (previous_capture_id<>current_capture_id),
  UNIQUE (account,sku,previous_capture_id,current_capture_id),
  UNIQUE (order_id,sku)
);

CREATE INDEX IF NOT EXISTS mp_fbo_intraday_link_lookup
  ON mp_fbo_intraday_link_candidates(account,sku,source_warehouse);

-- поток: fin. Read-only marketplace evidence, never COGS or marketplace mutations.
ALTER TABLE mp_fbo_intraday_link_candidates
  DROP CONSTRAINT IF EXISTS mp_fbo_intraday_link_candidates_account_check;
ALTER TABLE mp_fbo_intraday_link_candidates
  ADD CONSTRAINT mp_fbo_intraday_link_candidates_account_check
  CHECK(account IN ('wb_acc1','wb_acc2'));

CREATE TABLE IF NOT EXISTS ozon_fbo_intraday_capture (
 id bigserial PRIMARY KEY,
 account text NOT NULL CHECK(account IN ('oz_acc1','oz_acc2')),
 started_at timestamptz NOT NULL, completed_at timestamptz NOT NULL,
 source_rows integer NOT NULL CHECK(source_rows>=0),
 source_api text NOT NULL,
 CHECK(completed_at>=started_at), UNIQUE(account,started_at)
);
CREATE INDEX IF NOT EXISTS ozon_fbo_intraday_recent ON ozon_fbo_intraday_capture(account,completed_at DESC);
CREATE TABLE IF NOT EXISTS ozon_fbo_intraday_item (
 capture_id bigint NOT NULL REFERENCES ozon_fbo_intraday_capture(id),
 sku text NOT NULL, warehouse text NOT NULL,
 free_to_sell numeric NOT NULL CHECK(free_to_sell>=0),
 reserved numeric NOT NULL CHECK(reserved>=0),
 promised numeric NOT NULL CHECK(promised>=0),
 raw_payload jsonb NOT NULL,
 PRIMARY KEY(capture_id,sku,warehouse)
);
CREATE TABLE IF NOT EXISTS ozon_fbo_stock_link_candidates (
 id bigserial PRIMARY KEY,
 account text NOT NULL CHECK(account IN ('oz_acc1','oz_acc2')),
 order_id bigint NOT NULL REFERENCES mp_fbo_orders(id),
 sku text NOT NULL, source_warehouse text NOT NULL,
 previous_capture_id bigint NOT NULL REFERENCES ozon_fbo_intraday_capture(id),
 current_capture_id bigint NOT NULL REFERENCES ozon_fbo_intraday_capture(id),
 quantity numeric NOT NULL CHECK(quantity>0), evidence jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(previous_capture_id<>current_capture_id),
 UNIQUE(account,sku,source_warehouse,previous_capture_id,current_capture_id),
 UNIQUE(order_id,sku)
);

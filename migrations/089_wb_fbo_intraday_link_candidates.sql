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

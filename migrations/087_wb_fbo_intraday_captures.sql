-- поток: fin. Timestamped, immutable WB FBO inventory observations for the acc1 pilot.
-- The API does not provide the exact effective timestamp of its stock data.
CREATE TABLE IF NOT EXISTS wb_fbo_intraday_capture (
  id bigserial PRIMARY KEY,
  account text NOT NULL CHECK (account IN ('wb_acc1','wb_acc2')),
  started_at timestamptz NOT NULL,
  completed_at timestamptz NOT NULL,
  source_api text NOT NULL,
  source_rows integer NOT NULL CHECK (source_rows>0),
  quantity numeric NOT NULL CHECK (quantity>=0),
  in_way_to_client numeric NOT NULL CHECK (in_way_to_client>=0),
  in_way_from_client numeric NOT NULL CHECK (in_way_from_client>=0),
  CHECK (completed_at>=started_at),
  UNIQUE(account,started_at)
);
CREATE INDEX IF NOT EXISTS wb_fbo_intraday_capture_recent
  ON wb_fbo_intraday_capture(account,completed_at DESC);

CREATE TABLE IF NOT EXISTS wb_fbo_intraday_item (
  capture_id bigint NOT NULL REFERENCES wb_fbo_intraday_capture(id) ON DELETE CASCADE,
  nm_id bigint NOT NULL,
  chrt_id bigint NOT NULL,
  warehouse_id bigint NOT NULL,
  warehouse_name text NOT NULL,
  region_name text,
  quantity numeric NOT NULL CHECK (quantity>=0),
  in_way_to_client numeric NOT NULL CHECK (in_way_to_client>=0),
  in_way_from_client numeric NOT NULL CHECK (in_way_from_client>=0),
  raw_payload jsonb NOT NULL,
  PRIMARY KEY(capture_id,nm_id,chrt_id,warehouse_id)
);
CREATE INDEX IF NOT EXISTS wb_fbo_intraday_item_sku
  ON wb_fbo_intraday_item(nm_id,capture_id);

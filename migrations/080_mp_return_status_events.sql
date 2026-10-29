-- поток: fin. Observed marketplace return states; no inferred physical receipt.
CREATE TABLE IF NOT EXISTS mp_return_status_events (
  id bigserial PRIMARY KEY,
  platform text NOT NULL,
  account text NOT NULL,
  return_id text NOT NULL,
  source text NOT NULL,
  order_number text,
  return_type text,
  scheme text,
  status_raw text,
  status_name text,
  stage text,
  status_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  place_name text,
  target_name text,
  source_changed_at text,
  observed_at timestamptz NOT NULL,
  status_hash text NOT NULL,
  is_baseline boolean NOT NULL DEFAULT false,
  UNIQUE(platform,account,return_id,observed_at)
);
CREATE INDEX IF NOT EXISTS mp_return_status_events_order_idx
  ON mp_return_status_events(platform,account,return_id,id DESC);
CREATE INDEX IF NOT EXISTS mp_return_status_events_observed_idx
  ON mp_return_status_events(observed_at DESC);

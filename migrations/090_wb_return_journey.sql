-- поток: fin. Inferred return route from WB stock counters; no WB movement ID or COGS.
CREATE TABLE IF NOT EXISTS wb_return_journey_candidate (
  id bigserial PRIMARY KEY,
  account text NOT NULL CHECK (account IN ('wb_acc1','wb_acc2')),
  order_kind text NOT NULL CHECK (order_kind IN ('fbs','fbo')),
  order_id bigint NOT NULL,
  posting_number text NOT NULL,
  sku text NOT NULL,
  source_kind text NOT NULL CHECK (source_kind IN ('daily','intraday')),
  previous_observed_at timestamptz NOT NULL,
  current_observed_at timestamptz NOT NULL,
  transit_before numeric NOT NULL CHECK (transit_before>=0),
  transit_after numeric NOT NULL CHECK (transit_after=transit_before+1),
  stage text NOT NULL DEFAULT 'in_transit_candidate'
    CHECK (stage IN ('in_transit_candidate','received_wb','received_seller')),
  destination_observed_at timestamptz,
  evidence jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (previous_observed_at<current_observed_at),
  UNIQUE (order_kind,order_id,sku),
  UNIQUE (account,sku,source_kind,previous_observed_at,current_observed_at)
);
CREATE INDEX IF NOT EXISTS wb_return_journey_by_sku
  ON wb_return_journey_candidate(account,sku,stage);

CREATE TABLE IF NOT EXISTS wb_return_journey_event (
  id bigserial PRIMARY KEY,
  candidate_id bigint NOT NULL REFERENCES wb_return_journey_candidate(id),
  stage text NOT NULL CHECK (stage IN ('in_transit_candidate','received_wb','received_seller')),
  observed_at timestamptz NOT NULL,
  evidence jsonb NOT NULL,
  UNIQUE (candidate_id,stage)
);

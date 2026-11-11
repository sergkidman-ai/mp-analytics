-- поток: fin. Independent audit tables: safe to create before order/source tables.
-- History is retained independently of source-table lifecycle; no accounting views changed.
CREATE TABLE IF NOT EXISTS mp_fbo_ms_cost_proof (
 demand_id text PRIMARY KEY, checked_at timestamptz NOT NULL,
 document jsonb NOT NULL, cost_report jsonb NOT NULL, proof jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS mp_fbo_ms_cost_proof_event (
 id bigserial PRIMARY KEY,demand_id text NOT NULL,proof_hash text NOT NULL,
 checked_at timestamptz NOT NULL,document jsonb NOT NULL,cost_report jsonb NOT NULL,proof jsonb NOT NULL,
 UNIQUE(demand_id,proof_hash)
);
CREATE TABLE IF NOT EXISTS mp_fbo_source_lookup (
 account text NOT NULL, fbs_posting_number text NOT NULL,
 checked_at timestamptz NOT NULL DEFAULT now(), demand_id text, error text,
 PRIMARY KEY(account,fbs_posting_number)
);
CREATE TABLE IF NOT EXISTS mp_fbo_trace_state (
 order_id bigint PRIMARY KEY,
 state text NOT NULL CHECK(state IN ('unconfirmed','confirmed','conflict')),
 cost numeric, evidence jsonb NOT NULL, version_hash text NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK((state='confirmed' AND cost IS NOT NULL AND cost>0 AND evidence ? 'unit_id'
        AND evidence ? 'source_shipment_id' AND evidence ? 'cost_report_hash')
       OR (state<>'confirmed' AND cost IS NULL))
);
CREATE TABLE IF NOT EXISTS mp_fbo_trace_event (
 id bigserial PRIMARY KEY, order_id bigint NOT NULL,
 source_kind text NOT NULL, source_id text NOT NULL,
 event_hash text NOT NULL, effective_at timestamptz NOT NULL,
 observed_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL,
 UNIQUE(order_id,source_kind,source_id,event_hash)
);
CREATE INDEX IF NOT EXISTS mp_fbo_trace_event_history ON mp_fbo_trace_event(order_id,effective_at,id);
CREATE TABLE IF NOT EXISTS mp_fbo_return_origin (
 platform text NOT NULL CHECK(platform IN ('wb','ozon')),
 account text NOT NULL, source_key text NOT NULL, fbs_posting_number text NOT NULL,
 sku text NOT NULL, quantity numeric NOT NULL CHECK(quantity>0),
 stage text NOT NULL, received_at timestamptz,
 original_cost numeric, cost_kind text NOT NULL, evidence jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(platform,account,source_key),
 CHECK(original_cost IS NULL OR original_cost>0)
);
CREATE TABLE IF NOT EXISTS mp_fbo_return_origin_event (
 id bigserial PRIMARY KEY, platform text NOT NULL, account text NOT NULL, source_key text NOT NULL,
 event_hash text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL,
 UNIQUE(platform,account,source_key,event_hash)
);

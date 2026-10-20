-- поток: fin. Dedicated durable queue; no existing financial tables are written.
BEGIN;
CREATE TABLE IF NOT EXISTS ms_webhook_inbox (
 account_id uuid NOT NULL, request_id uuid NOT NULL, payload jsonb,
 transport text NOT NULL, delivery_metadata jsonb NOT NULL DEFAULT '{}',
 event_at timestamptz, received_at timestamptz NOT NULL DEFAULT now(),
 duplicate_count integer NOT NULL DEFAULT 0, last_duplicate_at timestamptz,
 PRIMARY KEY(account_id, request_id)
);
CREATE TABLE IF NOT EXISTS ms_shipment_jobs (
 account_id uuid NOT NULL, demand_id uuid NOT NULL,
 requested_generation bigint NOT NULL DEFAULT 1, processed_generation bigint NOT NULL DEFAULT 0,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','retry','done','error')),
 next_attempt_at timestamptz NOT NULL DEFAULT now(), attempts integer NOT NULL DEFAULT 0,
 locked_until timestamptz, lease_token uuid, last_error text,
 first_source text NOT NULL, last_source text NOT NULL,
 first_seen_at timestamptz NOT NULL DEFAULT now(), requested_at timestamptz NOT NULL DEFAULT now(),
 event_at timestamptz, observed_version text, processed_at timestamptz,
 last_checked_at timestamptz, fifo_checked_at timestamptz,
 PRIMARY KEY(account_id,demand_id)
);
CREATE INDEX IF NOT EXISTS ms_shipment_jobs_ready ON ms_shipment_jobs(next_attempt_at) WHERE state IN ('pending','retry','running');
CREATE TABLE IF NOT EXISTS ms_shipment_assessments (
 id bigserial PRIMARY KEY, account_id uuid NOT NULL, demand_id uuid NOT NULL,
 generation bigint NOT NULL, fingerprint text NOT NULL, document_version text,
 document_name text, document_moment timestamptz, platform text, account text, organization text,
 order_id text, status text NOT NULL, signals jsonb NOT NULL DEFAULT '[]',
 result jsonb NOT NULL, raw_document jsonb, raw_fifo jsonb, raw_order jsonb,
 discovery_source text NOT NULL, event_at timestamptz, received_at timestamptz,
 calculated_at timestamptz NOT NULL DEFAULT now(), is_current boolean NOT NULL DEFAULT false,
 UNIQUE(account_id,demand_id,fingerprint)
);
CREATE UNIQUE INDEX IF NOT EXISTS ms_shipment_current ON ms_shipment_assessments(account_id,demand_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS ms_shipment_assessments_filter ON ms_shipment_assessments(platform,account,document_moment) WHERE is_current;
CREATE TABLE IF NOT EXISTS ms_shipment_sync_state (
 account_id uuid PRIMARY KEY, checkpoint timestamptz NOT NULL,
 last_success_at timestamptz, last_attempt_at timestamptz, last_error text,
 window_upper timestamptz
);
CREATE TABLE IF NOT EXISTS ms_shipment_rules (
 account text PRIMARY KEY, platform text NOT NULL, organization_id uuid NOT NULL,
 retention numeric NOT NULL CHECK(retention >= 0 AND retention < 1),
 base_from date NOT NULL, base_to date NOT NULL, valid_until date NOT NULL,
 source text NOT NULL, source_sha256 text NOT NULL, version text NOT NULL,
 enabled boolean NOT NULL DEFAULT true, loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ms_shipment_metrics (
 name text PRIMARY KEY, value numeric NOT NULL DEFAULT 0, details jsonb NOT NULL DEFAULT '{}', updated_at timestamptz NOT NULL DEFAULT now()
);
COMMIT;

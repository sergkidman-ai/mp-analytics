-- поток: fin. Only composition/raw from TC; never catalogue cost or buy price.
CREATE TABLE IF NOT EXISTS ms_shipment_bom (
 external_code text PRIMARY KEY,
 components jsonb NOT NULL,
 kind text NOT NULL CHECK(kind IN ('set','not_mix')),
 source text NOT NULL,
 source_sha256 text NOT NULL,
 raw_payload jsonb NOT NULL,
 resolved_at timestamptz NOT NULL DEFAULT now(),
 valid_until timestamptz NOT NULL
);

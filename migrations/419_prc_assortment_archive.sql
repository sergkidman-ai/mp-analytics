-- поток: prc. Keep excluded work-queue records and suggestions without global SKU blacklisting.
CREATE TABLE IF NOT EXISTS prc_assortment_excluded (
 id bigserial PRIMARY KEY,
 entity_type text NOT NULL CHECK(entity_type IN ('novelty','unlinked')),
 entity_id text NOT NULL,
 supplier_key text,
 article text,
 name text NOT NULL,
 reason text NOT NULL,
 rules_version text NOT NULL,
 row_data jsonb NOT NULL,
 candidates jsonb NOT NULL,
 archived_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prc_assortment_excluded_identity ON prc_assortment_excluded(entity_type,entity_id);

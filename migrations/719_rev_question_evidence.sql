-- поток: rev. Technical evidence only; existing operational tables remain intact.
CREATE TABLE IF NOT EXISTS rev_fact_source (
    kind text NOT NULL, source_key text NOT NULL, payload jsonb NOT NULL,
    fingerprint text NOT NULL, source_at timestamptz, checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(kind,source_key)
);
CREATE TABLE IF NOT EXISTS rev_question_evidence (
    platform text NOT NULL, account text NOT NULL, kind text NOT NULL, ext_id text NOT NULL,
    version text NOT NULL, native_signature text NOT NULL, fingerprint text NOT NULL,
    payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(platform,account,kind,ext_id)
);
CREATE TABLE IF NOT EXISTS rev_fact_cursor (
    name text PRIMARY KEY, page integer NOT NULL DEFAULT 1,
    completed_at timestamptz, last_error text, updated_at timestamptz NOT NULL DEFAULT now()
);

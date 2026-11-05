-- поток: rev. Explicitly enrolled historical cases; analysis does not enable publication.
CREATE TABLE IF NOT EXISTS rev_history_analysis (
 platform text NOT NULL, account text NOT NULL, kind text NOT NULL, ext_id text NOT NULL,
 state text NOT NULL DEFAULT 'pending', classification text NOT NULL,
 source_hash text NOT NULL, error_type text,
 enrolled_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(platform,account,kind,ext_id)
);

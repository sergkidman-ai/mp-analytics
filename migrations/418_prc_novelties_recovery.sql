-- поток: prc. Restore observation tables; dates are evidence, never migration timestamps.

CREATE TABLE IF NOT EXISTS prc_launch (
  external_code text PRIMARY KEY,
  ms_created    date,
  ms_source     text NOT NULL,
  ms_name       text,
  supplier_key  text,
  cards         int  NOT NULL DEFAULT 0,
  archived      boolean NOT NULL DEFAULT false,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prc_launch_mp (
  external_code text NOT NULL,
  showcase      text NOT NULL,
  state         text NOT NULL,
  card_at       date,
  selling_at    date,
  seeded        boolean NOT NULL DEFAULT false,
  offer         text,
  note          text,
  checked_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (external_code, showcase)
);
CREATE INDEX IF NOT EXISTS prc_launch_created_idx ON prc_launch (ms_created);

CREATE TABLE IF NOT EXISTS prc_launch_observation (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 baseline_date date NOT NULL,
 restored_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prc_launch_creation_evidence (
 external_code text PRIMARY KEY,
 ms_created date NOT NULL,
 source_ref text NOT NULL
);
CREATE TABLE IF NOT EXISTS prc_launch_source_state (
 showcase text PRIMARY KEY,
 status text NOT NULL,
 note text,
 checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prc_recovery_evidence (
 evidence_id text PRIMARY KEY,
 kind text NOT NULL,
 article text,
 supplier_key text,
 source_ref text NOT NULL,
 payload jsonb NOT NULL,
 applied boolean NOT NULL DEFAULT false,
 applied_at timestamptz
);

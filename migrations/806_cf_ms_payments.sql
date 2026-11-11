-- поток: cf. Read-only mirror of MS payments; bank/inv tables remain untouched.
CREATE TABLE IF NOT EXISTS cf_ms_payment (
    entity text NOT NULL CHECK (entity IN ('paymentin', 'paymentout')),
    id text NOT NULL,
    org_inn text NOT NULL,
    day date NOT NULL,
    payload jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity, id)
);
CREATE INDEX IF NOT EXISTS cf_ms_payment_org_day ON cf_ms_payment(org_inn, day);
CREATE TABLE IF NOT EXISTS cf_ms_payment_sync (
    org_inn text PRIMARY KEY,
    date_from date NOT NULL,
    date_to date NOT NULL,
    row_count integer NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);

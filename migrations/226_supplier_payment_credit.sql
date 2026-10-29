-- поток: inv
-- Register a real, unallocated MS supplier payment for reuse by the SAME organization.
CREATE TABLE IF NOT EXISTS supplier_payment_credit (
    ms_payment_id uuid PRIMARY KEY,
    org_inn text NOT NULL,
    inn text NOT NULL,
    credit_kop bigint NOT NULL CHECK (credit_kop > 0),
    baseline_operations jsonb NOT NULL,
    note text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    checked_at timestamptz,
    remaining_kop bigint CHECK (remaining_kop >= 0)
);
CREATE INDEX IF NOT EXISTS supplier_payment_credit_org_supplier
    ON supplier_payment_credit(org_inn, inn);

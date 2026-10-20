-- поток: fin. Immutable expense snapshots, publication state and original rule history.
CREATE TABLE IF NOT EXISTS fin_ozon_expense_basis (
 version TEXT PRIMARY KEY, account TEXT NOT NULL,
 period_from DATE NOT NULL, period_to DATE NOT NULL,
 status TEXT NOT NULL, payload JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fin_ozon_basis_period_idx ON fin_ozon_expense_basis(account,period_from DESC);
CREATE TABLE IF NOT EXISTS fin_ozon_expense_current (
 account TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES fin_ozon_expense_basis(version),
 checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fin_ozon_rule_history (
 id BIGSERIAL PRIMARY KEY, account TEXT NOT NULL, version TEXT,
 payload JSONB NOT NULL, captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(account,payload)
);
CREATE OR REPLACE FUNCTION fin_ozon_basis_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Ozon expense basis versions are immutable; publish a new version'; END; $$;
DROP TRIGGER IF EXISTS fin_ozon_basis_immutable ON fin_ozon_expense_basis;
CREATE TRIGGER fin_ozon_basis_immutable BEFORE UPDATE OR DELETE ON fin_ozon_expense_basis
 FOR EACH ROW EXECUTE FUNCTION fin_ozon_basis_immutable();
CREATE OR REPLACE FUNCTION fin_ozon_preserve_rule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.platform='ozon' THEN
  INSERT INTO fin_ozon_rule_history(account,version,payload)
  VALUES(OLD.account,OLD.version,to_jsonb(OLD)) ON CONFLICT DO NOTHING;
 END IF;
 RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END; $$;
DO $$ BEGIN
 IF to_regclass('ms_shipment_rules') IS NOT NULL THEN
  EXECUTE 'DROP TRIGGER IF EXISTS fin_ozon_preserve_rule ON ms_shipment_rules';
  EXECUTE 'CREATE TRIGGER fin_ozon_preserve_rule BEFORE UPDATE OR DELETE ON ms_shipment_rules FOR EACH ROW EXECUTE FUNCTION fin_ozon_preserve_rule()';
 END IF;
END; $$;

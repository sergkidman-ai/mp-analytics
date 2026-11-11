-- поток: inv. Зафиксированные закупки: суммы и исходные документы месяца.
CREATE TABLE IF NOT EXISTS supplier_purchase_period (
    month date PRIMARY KEY CHECK (extract(day FROM month)=1),
    closed_at timestamptz,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    sha256 text NOT NULL CHECK (length(sha256)=64),
    payload jsonb NOT NULL
);

CREATE OR REPLACE FUNCTION supplier_purchase_history_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(224231);
    IF TG_TABLE_NAME='supplier_purchase_period' THEN
        IF TG_OP<>'INSERT' AND OLD.closed_at IS NOT NULL THEN
            RAISE EXCEPTION 'Supplier purchase month is closed';
        END IF;
        IF TG_OP<>'DELETE' AND NEW.closed_at IS NOT NULL AND
           NEW.month >= date_trunc('month',now() AT TIME ZONE 'Europe/Moscow')::date THEN
            RAISE EXCEPTION 'Current or future supplier month cannot be closed';
        END IF;
    ELSE
        IF TG_OP<>'INSERT' AND EXISTS (
            SELECT 1 FROM supplier_purchase_period WHERE month=OLD.month AND closed_at IS NOT NULL
        ) THEN RAISE EXCEPTION 'Supplier purchase month is closed'; END IF;
        IF TG_OP<>'DELETE' AND EXISTS (
            SELECT 1 FROM supplier_purchase_period WHERE month=NEW.month AND closed_at IS NOT NULL
        ) THEN RAISE EXCEPTION 'Supplier purchase month is closed'; END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS supplier_purchase_month_guard ON supplier_purchase_month;
CREATE TRIGGER supplier_purchase_month_guard BEFORE INSERT OR UPDATE OR DELETE
ON supplier_purchase_month FOR EACH ROW EXECUTE FUNCTION supplier_purchase_history_guard();
DROP TRIGGER IF EXISTS supplier_purchase_period_guard ON supplier_purchase_period;
CREATE TRIGGER supplier_purchase_period_guard BEFORE INSERT OR UPDATE OR DELETE
ON supplier_purchase_period FOR EACH ROW EXECUTE FUNCTION supplier_purchase_history_guard();

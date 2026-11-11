-- поток: inv. Versioned, immutable snapshots of closed OPEX months.
CREATE TABLE IF NOT EXISTS opex_month_closure (
 id bigserial PRIMARY KEY, org_inn text NOT NULL, month date NOT NULL,
 closed_at timestamptz NOT NULL DEFAULT now(), reopened_at timestamptz,
 statement jsonb NOT NULL, summary jsonb NOT NULL, rules_snapshot jsonb NOT NULL,
 snapshot_sha256 text NOT NULL, CHECK(extract(day from month)=1)
);
CREATE UNIQUE INDEX IF NOT EXISTS opex_month_closure_active ON opex_month_closure(org_inn,month) WHERE reopened_at IS NULL;
CREATE TABLE IF NOT EXISTS opex_closed_txn (
 close_id bigint NOT NULL REFERENCES opex_month_closure(id), txn_id bigint NOT NULL,
 category_id integer, category text, in_opex boolean, source text,
 start_month date, spread_months integer, PRIMARY KEY(close_id,txn_id)
);
CREATE TABLE IF NOT EXISTS opex_closed_alloc (
 close_id bigint NOT NULL REFERENCES opex_month_closure(id), txn_id bigint NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(close_id,txn_id)
);
CREATE TABLE IF NOT EXISTS opex_closed_item (
 close_id bigint NOT NULL REFERENCES opex_month_closure(id), category_id integer NOT NULL,
 category text NOT NULL, amount numeric NOT NULL, txn_count integer NOT NULL,
 PRIMARY KEY(close_id,category_id)
);
CREATE OR REPLACE VIEW opex_fact_alloc_live AS
SELECT (a.start_month + (g.i || ' month')::interval)::date AS month,
       t.id            AS txn_id,
       t.org_inn,
       t.operation_date,
       t.cp_name,
       t.purpose,
       t.amount        AS txn_amount,
       a.spread_months,
       a.category_id,
       c.name          AS category,
       CASE WHEN g.i = a.spread_months - 1
            THEN t.amount - round(t.amount / a.spread_months, 2) * (a.spread_months - 1)
            ELSE round(t.amount / a.spread_months, 2)
       END             AS amount
FROM bank_txn_opex a
JOIN bank_txn      t ON t.id = a.txn_id
JOIN opex_category c ON c.id = a.category_id
CROSS JOIN LATERAL generate_series(0, a.spread_months - 1) AS g(i)
WHERE t.direction = 'DEBIT'
  AND c.in_opex;

CREATE OR REPLACE VIEW opex_fact_alloc AS
SELECT r.* FROM opex_closed_alloc s JOIN opex_month_closure h ON h.id=s.close_id
 CROSS JOIN LATERAL jsonb_populate_record(NULL::opex_fact_alloc_live,s.payload) r
 WHERE h.reopened_at IS NULL
UNION ALL
SELECT l.* FROM opex_fact_alloc_live l WHERE NOT EXISTS (
 SELECT 1 FROM opex_month_closure h WHERE h.org_inn=l.org_inn AND h.month=l.month AND h.reopened_at IS NULL);
CREATE OR REPLACE VIEW opex_fact_month AS
SELECT h.month,h.org_inn,s.category_id,s.category,s.amount,s.txn_count
 FROM opex_closed_item s JOIN opex_month_closure h ON h.id=s.close_id WHERE h.reopened_at IS NULL
UNION ALL
SELECT l.month,l.org_inn,l.category_id,l.category,sum(l.amount) AS amount,count(*)::integer AS txn_count
 FROM opex_fact_alloc_live l WHERE NOT EXISTS (
 SELECT 1 FROM opex_month_closure h WHERE h.org_inn=l.org_inn AND h.month=l.month AND h.reopened_at IS NULL)
 GROUP BY l.month,l.org_inn,l.category_id,l.category;
CREATE OR REPLACE FUNCTION opex_assignment_locked(p_txn bigint,p_start date,p_n integer)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS(SELECT 1 FROM opex_month_closure h JOIN bank_txn t ON t.org_inn=h.org_inn
 WHERE t.id=p_txn AND h.reopened_at IS NULL AND
 (h.month=date_trunc('month',t.operation_date)::date OR
  (p_start IS NOT NULL AND h.month>=p_start AND h.month<p_start+(p_n||' month')::interval)))
$$;
CREATE OR REPLACE FUNCTION opex_guard_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE blocked boolean := false;
BEGIN
 IF TG_OP<>'INSERT' THEN blocked:=opex_assignment_locked(OLD.txn_id,OLD.start_month,OLD.spread_months); END IF;
 IF TG_OP<>'DELETE' THEN blocked:=blocked OR opex_assignment_locked(NEW.txn_id,NEW.start_month,NEW.spread_months); END IF;
 IF blocked THEN
  IF (TG_OP='DELETE' AND OLD.source='rule') OR (TG_OP<>'DELETE' AND NEW.source='rule') THEN RETURN NULL; END IF;
  RAISE EXCEPTION 'Месяц расходов закрыт. Откройте месяц для изменения разметки или периода.' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS opex_closed_assignment_guard ON bank_txn_opex;
CREATE TRIGGER opex_closed_assignment_guard BEFORE INSERT OR UPDATE OR DELETE ON bank_txn_opex
 FOR EACH ROW EXECUTE FUNCTION opex_guard_assignment();
CREATE OR REPLACE FUNCTION opex_guard_bank_txn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM opex_month_closure h WHERE h.reopened_at IS NULL AND
 h.org_inn=OLD.org_inn AND h.month=date_trunc('month',OLD.operation_date)::date)
 OR EXISTS(SELECT 1 FROM bank_txn_opex a WHERE a.txn_id=OLD.id AND opex_assignment_locked(a.txn_id,a.start_month,a.spread_months)) THEN
  RAISE EXCEPTION 'Выписка закрытого месяца сохранена. Для исправления откройте месяц.' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM opex_month_closure h WHERE h.reopened_at IS NULL AND
 h.org_inn=NEW.org_inn AND h.month=date_trunc('month',NEW.operation_date)::date) THEN
  RAISE EXCEPTION 'Нельзя перенести операцию в закрытый месяц.' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS opex_closed_bank_guard ON bank_txn;
CREATE TRIGGER opex_closed_bank_guard BEFORE UPDATE OR DELETE ON bank_txn
 FOR EACH ROW EXECUTE FUNCTION opex_guard_bank_txn();
CREATE OR REPLACE FUNCTION opex_guard_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Сохранённый снимок расходов неизменяем. Для правки откройте месяц и создайте новую версию.' USING ERRCODE='23514';
END $$;
DROP TRIGGER IF EXISTS opex_snapshot_guard ON opex_closed_txn;
CREATE TRIGGER opex_snapshot_guard BEFORE UPDATE OR DELETE ON opex_closed_txn FOR EACH ROW EXECUTE FUNCTION opex_guard_snapshot();
DROP TRIGGER IF EXISTS opex_snapshot_guard ON opex_closed_alloc;
CREATE TRIGGER opex_snapshot_guard BEFORE UPDATE OR DELETE ON opex_closed_alloc FOR EACH ROW EXECUTE FUNCTION opex_guard_snapshot();
DROP TRIGGER IF EXISTS opex_snapshot_guard ON opex_closed_item;
CREATE TRIGGER opex_snapshot_guard BEFORE UPDATE OR DELETE ON opex_closed_item FOR EACH ROW EXECUTE FUNCTION opex_guard_snapshot();
CREATE OR REPLACE FUNCTION opex_guard_closure_header() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'История закрытия расходов сохраняется.' USING ERRCODE='23514'; END IF;
 IF ROW(NEW.id,NEW.org_inn,NEW.month,NEW.closed_at,NEW.statement,NEW.summary,NEW.rules_snapshot,NEW.snapshot_sha256)
 IS DISTINCT FROM ROW(OLD.id,OLD.org_inn,OLD.month,OLD.closed_at,OLD.statement,OLD.summary,OLD.rules_snapshot,OLD.snapshot_sha256)
 OR OLD.reopened_at IS NOT NULL OR NEW.reopened_at IS NULL THEN
  RAISE EXCEPTION 'Снимок закрытого месяца неизменяем.' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS opex_closure_header_guard ON opex_month_closure;
CREATE TRIGGER opex_closure_header_guard BEFORE UPDATE OR DELETE ON opex_month_closure FOR EACH ROW EXECUTE FUNCTION opex_guard_closure_header();

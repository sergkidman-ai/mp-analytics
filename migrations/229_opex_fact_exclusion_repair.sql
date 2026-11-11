-- поток: inv
-- Recovery retained the original 209 view while restoring the category flags of 219.
-- Restore the shared OPEX exclusion; cashflow reads payments directly and is unaffected.
-- No changes to categories, payment assignments, allocation periods or raw transactions.
CREATE OR REPLACE VIEW opex_fact_alloc AS
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
  AND c.in_opex;                       -- 219: нерасходные статьи в факт опер. расходов не идут

COMMENT ON VIEW opex_fact_alloc IS
    'Разнесённый по месяцам факт опер. расходов; статьи с in_opex=false исключены (219)';

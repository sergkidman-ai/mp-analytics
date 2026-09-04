-- поток: inv
-- 219: статья расходов, которая НЕ входит в операционные расходы.
--
-- Задача Сергея 04.09.2026: «висят платежи якобы не размеченные, хотя это всё поставщики».
-- До сих пор статья была одна на все случаи: любой платёж либо попадал в опер. расходы, либо
-- висел без статьи. Оплата поставщику товара и перекладывание денег между своими счетами —
-- не опер. расход (это закупка и внутреннее движение), но и не «не разнесено»: разносить там
-- нечего. Они забивали счётчик «Без статьи», и по нему было не видно, сколько РЕАЛЬНО осталось
-- разобрать.
--
-- Решение: у статьи появляется флаг `in_opex`. Статьи с false видны в разметке и в разрезе
-- по статьям, но в витрину факта (`opex_fact_alloc` → `opex_fact_month`, а через неё в раздел
-- «Опер. расходы» и в сравнение с планом) НЕ ПОПАДАЮТ.
--
-- Почему флаг на статье, а не отдельная таблица «исключений»: разметка платежа остаётся ОДНИМ
-- полем (`bank_txn_opex.category_id`), правила (`opex_rule`) работают без изменений, и любой
-- новый нерасходный поток заводится строкой справочника, а не кодом.

ALTER TABLE opex_category ADD COLUMN IF NOT EXISTS in_opex boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN opex_category.in_opex IS
    'Входит ли статья в операционные расходы. false — платёж размечен, но в витрину факта '
    'не идёт (закупка товара, переводы между своими счетами)';

INSERT INTO opex_category (name, sort, in_opex) VALUES
    ('Закупка товара у поставщиков', 900, false),
    ('Перевод между своими счетами', 910, false)
ON CONFLICT (name) DO UPDATE SET in_opex = EXCLUDED.in_opex, sort = EXCLUDED.sort;

-- Витрина факта: та же, что в 209, плюс отсечка по in_opex. Набор колонок не меняется,
-- поэтому CREATE OR REPLACE проходит без пересоздания зависимой opex_fact_month.
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

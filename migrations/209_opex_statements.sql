-- 209_opex_statements.sql — поток: inv. Банковская выписка в БД + разметка по статьям опер. расходов.
--
-- Зачем: до сих пор выписка обеих фирм нигде у нас не оседала — alfa_statement/sber_statement
-- нормализовали операции и лили их сразу в МойСклад (сырьё JSON — в incoming/). Вкладка
-- «Опер. расходы» при этом жила на РУЧНОМ помесячном снапшоте (таблица opex): человек набивал
-- ФОТ/аренду/налоги руками, факт с банком никак не сверялся.
--
-- Что делаем: складываем ПОЛНУЮ выписку (оба направления, все контрагенты, ничего не выбрасываем)
-- начиная с 01.08.2026, даём человеку разметить платёж статьёй расходов, запоминаем решение по
-- контрагенту (правило по ИНН) и раскладываем размеченное по месяцам.
--
-- Решения Сергея 03.08.2026:
--   1. Факт из выписки ЗАМЕЩАЕТ ручной снапшот с 08.2026; июль и раньше — прежняя ручная таблица.
--   2. Платежи поставщикам НЕ размечаются (это закупка товара, будущий раздел «Кэшфлоу») —
--      висят нераспределёнными и в итог опер. расходов не входят. Приход статьи не требует.
--   3. Память правил — по ИНН контрагента, любое назначение (фолбэк по имени, если ИНН пуст).
--   4. Разнесение платежа «на период вперёд» — РАВНЫМИ долями по N месяцам с месяца платежа.

-- ── выписка как есть ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bank_txn (
    id              BIGSERIAL PRIMARY KEY,
    bank            TEXT NOT NULL,          -- alfa | sber
    org_inn         TEXT NOT NULL,          -- 7807355364 Цифровой Квадрат | 7811803918 Дисквэр
    account         TEXT NOT NULL,          -- наш счёт списания/зачисления
    nk              TEXT NOT NULL,          -- натуральный ключ: uuid операции, иначе хеш реквизитов
    txn_uuid        TEXT,
    transaction_id  TEXT,
    direction       TEXT NOT NULL,          -- CREDIT (приход) | DEBIT (расход)
    amount          NUMERIC NOT NULL,
    currency        TEXT,
    operation_date  DATE NOT NULL,
    document_date   DATE,
    document_number TEXT,
    purpose         TEXT,
    cp_name         TEXT,                   -- контрагент: для DEBIT получатель, для CREDIT плательщик
    cp_inn          TEXT,
    cp_kpp          TEXT,
    cp_account      TEXT,
    cp_bic          TEXT,
    raw             JSONB,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bank, nk)
);
CREATE INDEX IF NOT EXISTS idx_bank_txn_org_date ON bank_txn (org_inn, operation_date);
CREATE INDEX IF NOT EXISTS idx_bank_txn_cp_inn   ON bank_txn (cp_inn) WHERE cp_inn IS NOT NULL;

COMMENT ON TABLE  bank_txn    IS 'Полная банковская выписка обеих организаций с 01.08.2026, как пришла из банка';
COMMENT ON COLUMN bank_txn.nk IS 'Натуральный ключ идемпотентности: uuid операции банка, иначе md5 реквизитов';

-- ── справочник статей опер. расходов ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS opex_category (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    sort     INT  NOT NULL DEFAULT 100,
    archived BOOLEAN NOT NULL DEFAULT false
);

INSERT INTO opex_category (name, sort) VALUES
    ('ФОТ', 10), ('Самозанятые', 20), ('Налоги и взносы', 30), ('Аренда', 40),
    ('Интернет и связь', 50), ('Банковское обслуживание', 60), ('Прочее', 900)
ON CONFLICT (name) DO NOTHING;

-- ── разметка платежа: статья + разнесение по месяцам ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS bank_txn_opex (
    txn_id        BIGINT PRIMARY KEY REFERENCES bank_txn(id) ON DELETE CASCADE,
    category_id   INT NOT NULL REFERENCES opex_category(id),
    spread_months INT NOT NULL DEFAULT 1 CHECK (spread_months >= 1 AND spread_months <= 120),
    start_month   DATE NOT NULL,            -- 1-е число месяца, с которого идёт разнесение
    source        TEXT NOT NULL DEFAULT 'manual',   -- manual (человек) | rule (сработало правило)
    note          TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bank_txn_opex_cat ON bank_txn_opex (category_id);

COMMENT ON COLUMN bank_txn_opex.spread_months IS
    '1 = весь платёж в месяц оплаты; N = равными долями на N месяцев с start_month (лицензия на год → 12)';

-- ── память: контрагент → статья (правило по ИНН, фолбэк по имени) ────────────────────────
CREATE TABLE IF NOT EXISTS opex_rule (
    id            SERIAL PRIMARY KEY,
    cp_inn        TEXT,
    cp_name_key   TEXT,                     -- нормализованное имя: нижний регистр, только буквы/цифры
    category_id   INT NOT NULL REFERENCES opex_category(id),
    spread_months INT NOT NULL DEFAULT 1 CHECK (spread_months >= 1 AND spread_months <= 120),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (coalesce(cp_inn, '') <> '' OR coalesce(cp_name_key, '') <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opex_rule_inn  ON opex_rule (cp_inn)      WHERE coalesce(cp_inn, '') <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_opex_rule_name ON opex_rule (cp_name_key) WHERE coalesce(cp_name_key, '') <> '';

COMMENT ON TABLE opex_rule IS
    'Решение человека, запомненное на будущее: все платежи этому контрагенту идут в эту статью';

-- ── раскладка размеченных платежей по месяцам ────────────────────────────────────────────
-- Доля = amount/N с округлением до копейки; ОСТАТОК копеек падает в ПОСЛЕДНИЙ месяц,
-- чтобы сумма долей была ровно равна платежу. Считаем только расход (DEBIT).
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
WHERE t.direction = 'DEBIT';

CREATE OR REPLACE VIEW opex_fact_month AS
SELECT month, org_inn, category_id, category,
       sum(amount)::numeric AS amount,
       count(*)::int        AS txn_count
FROM opex_fact_alloc
GROUP BY month, org_inn, category_id, category;

COMMENT ON VIEW opex_fact_month IS
    'Факт опер. расходов по месяцам из размеченной выписки (замещает ручной снапшот opex с 08.2026)';

-- поток: cf
-- 801_cashflow.sql — раздел «Кэшфлоу»: понедельный ДДС раздельно по юрлицам (факт + план).
-- Чужие данные (bank_txn, po_payment_status, rent_plan, raw_wb_report, raw_ozon_*, yandex_*)
-- только читаем; здесь — свои таблицы потока cf.

-- Остатки на счетах на конец дня. api — Альфа/Сбер (statement/summary), manual — опорный
-- остаток из выписки Озон Банка (API нет, файлы не хранятся), дальше катим операциями.
CREATE TABLE IF NOT EXISTS cf_balance (
    bank        text        NOT NULL,              -- alfa | sber | ozon
    account     text        NOT NULL,
    org_inn     text        NOT NULL,
    bal_date    date        NOT NULL,              -- остаток на КОНЕЦ этого дня
    balance     numeric(14,2) NOT NULL,
    source      text        NOT NULL DEFAULT 'api', -- api | manual
    note        text,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bank, account, bal_date)
);

-- Классификация поступлений (CREDIT) по контрагенту/назначению. Первое совпадение по prio.
CREATE TABLE IF NOT EXISTS cf_income_rule (
    id          serial PRIMARY KEY,
    prio        int         NOT NULL DEFAULT 100,
    cp_inn      text,                              -- точное совпадение
    purpose_re  text,                              -- regex по назначению (без учёта регистра)
    category    text        NOT NULL,              -- mp_wb | mp_ozon | mp_yandex | transfer | supplier_refund | loan | owner | other
    note        text
);

-- Плановые повторяющиеся платежи (зарплата, НДФЛ, налоги, программист, упаковка...).
-- Суммы стартуют со средних по факту (source=auto), правятся на странице (source=manual).
CREATE TABLE IF NOT EXISTS cf_plan_rule (
    id          serial PRIMARY KEY,
    org_inn     text        NOT NULL,
    category    text        NOT NULL,              -- ключ строки кэшфлоу (salary, ndfl, vat, usn, programmer, packaging, ...)
    name        text        NOT NULL,
    amount      numeric(14,2) NOT NULL,            -- сумма ОДНОГО платежа, положительная
    schedule    text        NOT NULL,              -- monthly | quarterly
    days        int[]       NOT NULL,              -- дни месяца (перенос на ближайший рабочий назад)
    months      int[],                             -- для quarterly: месяцы платежа
    active      boolean     NOT NULL DEFAULT true,
    source      text        NOT NULL DEFAULT 'auto',
    note        text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Витрина: строки кэшфлоу, пересобирается целиком ежедневно (cf/build.py).
CREATE TABLE IF NOT EXISTS cf_line (
    id          bigserial PRIMARY KEY,
    org_inn     text        NOT NULL,
    week_start  date        NOT NULL,              -- понедельник
    day         date        NOT NULL,
    kind        text        NOT NULL,              -- fact | plan
    category    text        NOT NULL,
    amount      numeric(14,2) NOT NULL,            -- со знаком: + поступление, − платёж
    source      text        NOT NULL,              -- bank | po_payment | rent_plan | plan_rule | wb_report | ozon_accrual | ...
    ref         text,                              -- id первичного документа
    note        text,
    estimate    boolean     NOT NULL DEFAULT false -- true — оценка, не документ
);
CREATE INDEX IF NOT EXISTS cf_line_org_week ON cf_line (org_inn, week_start);

-- Замороженный план (каждый понедельник) — для «план vs факт» по прошедшим неделям.
CREATE TABLE IF NOT EXISTS cf_plan_snapshot (
    snap_date   date        NOT NULL,
    org_inn     text        NOT NULL,
    week_start  date        NOT NULL,
    category    text        NOT NULL,
    amount      numeric(14,2) NOT NULL,
    PRIMARY KEY (snap_date, org_inn, week_start, category)
);

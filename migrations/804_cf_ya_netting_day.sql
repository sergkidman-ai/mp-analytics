-- поток: cf
-- 804_cf_ya_netting_day.sql — Маркет считает выплаты по КАЛЕНДАРНЫМ периодам (1–7, 8–14, 15–21,
-- 22–конец месяца; скрин ЛК 22.09.2026), а не по неделям пн–вс: храним по дню доставки.
-- Таблица 803 (по неделям) создана в тот же день и заменяется этой.
DROP TABLE IF EXISTS cf_ya_netting_week;
CREATE TABLE IF NOT EXISTS cf_ya_netting_day (
    account     text        NOT NULL,
    day         date        NOT NULL,              -- дата доставки заказа
    pending     numeric(14,2) NOT NULL DEFAULT 0,  -- ждёт выплаты: начислено − удержано, без платёжки
    paid        numeric(14,2) NOT NULL DEFAULT 0,  -- уже в платёжках
    rows_cnt    int         NOT NULL DEFAULT 0,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, day)
);

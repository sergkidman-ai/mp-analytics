-- поток: fin4
-- Ozon отключил /v3/finance/transaction/list (09.09.2026, code 9 «obsolete method»).
-- Замена — /v1/finance/accrual/by-day: сырьё начислений по дням. raw_ozon_transaction
-- остаётся историей до 08.09. Сверка 05.09 oz_acc1: 125 записей / 135 089 ₽ = старой таблице.
CREATE TABLE IF NOT EXISTS raw_ozon_accrual (
    id BIGSERIAL PRIMARY KEY,
    account TEXT NOT NULL,
    accrual_id BIGINT NOT NULL,
    accrual_date DATE NOT NULL,
    accrued_category TEXT,            -- ITEM / NON_ITEM / POSTING
    unit_number TEXT,                 -- номер отправления/единицы
    total_amount NUMERIC(14,2),
    payload JSONB NOT NULL,
    loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (account, accrual_id)
);
CREATE INDEX IF NOT EXISTS raw_ozon_accrual_date_idx ON raw_ozon_accrual (account, accrual_date);

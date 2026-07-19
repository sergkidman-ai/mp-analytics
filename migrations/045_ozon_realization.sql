-- migrations/045_ozon_realization.sql — «Отчёт о реализации товаров» Ozon (/v2/finance/realization).
-- Помесячный отчёт, который Ozon считает у себя: строка «Продажи» в ЛК раскладывается на
--   Выручка (delivery_commission.amount) + Баллы за скидки (bonus) +
--   Программы партнёров (bank_coinvestment + pick_up_point_coinvestment + stars).
-- Постинги (financial_data) этот сплит воспроизвести не могут — источник только этот отчёт.
-- Храним весь result (header+rows) одним payload на аккаунт+месяц (идемпотентно).
CREATE TABLE IF NOT EXISTS raw_ozon_realization (
    id BIGSERIAL PRIMARY KEY,
    account TEXT,
    year INT,
    month INT,
    payload JSONB NOT NULL,
    loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, year, month)
);

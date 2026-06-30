-- Яндекс.Маркет помесячно: выручка (payment, без отменённых), субсидия, заказы.
-- Источник: /campaigns/{id}/stats/orders (отдаёт историю, в отличие от business/orders ~30д).
CREATE TABLE IF NOT EXISTS yandex_monthly (
    account     TEXT NOT NULL,
    month       DATE NOT NULL,        -- первое число месяца
    revenue     NUMERIC,              -- Σ payment (наша цена, что заплатил покупатель), без CANCELLED
    subsidy     NUMERIC,              -- Σ субсидия Маркета (доплата сверху)
    orders      INT,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, month)
);

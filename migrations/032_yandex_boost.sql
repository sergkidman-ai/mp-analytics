-- 032: реклама Яндекс.Маркета по месяцам из отчётов продвижения.
-- AUCTION_PROMOTION в комиссиях заказа — лишь малая часть (атрибуция по заказам):
-- июнь 6.3к против реальных 126к буста продаж + 34к буста показов.
-- Источники: /reports/boost-consolidated/generate (буст продаж, BILLED_AMOUNT, без дат — по месяцу)
-- и /reports/shows-boost/generate (буст показов, REAL_COST по дням).

CREATE TABLE IF NOT EXISTS yandex_boost_monthly (
    account     text    NOT NULL,
    month       date    NOT NULL,
    sales_boost numeric,            -- буст продаж: Σ BILLED_AMOUNT
    shows_boost numeric,            -- буст показов: Σ REAL_COST
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, month)
);

-- 030: Яндекс.Маркет — сырьё stats/orders (полная экономика заказа) + финансовая витрина по месяцам.
-- stats/orders отдаёт историю с любых дат: payments, subsidies, commissions[] (FEE, DELIVERY_TO_CUSTOMER,
-- PAYMENT_TRANSFER=эквайринг, AUCTION_PROMOTION=буст, AGENCY), статусы (RETURNED и т.п.), items.shopSku.

CREATE TABLE IF NOT EXISTS raw_yandex_stats_order (
    account     text        NOT NULL,
    order_id    text        NOT NULL,
    campaign_id text        NOT NULL,
    payload     jsonb       NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, order_id)
);
CREATE INDEX IF NOT EXISTS idx_ya_stats_creation
    ON raw_yandex_stats_order ((payload->>'creationDate'));

CREATE TABLE IF NOT EXISTS yandex_finance_monthly (
    account        text    NOT NULL,
    month          date    NOT NULL,
    revenue        numeric,            -- Σ payments (что заплатил покупатель), без CANCELLED
    subsidy        numeric,            -- доплата Маркета сверху
    orders         integer,
    returns_orders integer,            -- статус RETURNED/PARTIALLY_RETURNED
    returns_sum    numeric,
    fee            numeric,            -- комиссия за размещение (FEE)
    delivery       numeric,            -- логистика (DELIVERY_TO_CUSTOMER и пр. DELIVERY_*)
    transfer       numeric,            -- эквайринг/перевод денег (PAYMENT_TRANSFER)
    promotion      numeric,            -- буст продаж (AUCTION_PROMOTION) = реклама
    agency         numeric,            -- агентское (AGENCY)
    other_fee      numeric,            -- остальные типы commissions
    cogs           numeric,            -- Σ qty × yandex_cost по shopSku (без отмен и возвратов)
    cogs_cov_pct   numeric,            -- % штук с известной себестоимостью
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, month)
);

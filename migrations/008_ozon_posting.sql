-- migrations/008_ozon_posting.sql — постинги Ozon (заказы) с financial_data: цены продажи,
-- цена до скидки, скидки/акции. Отдельно от транзакций (там цен нет).
CREATE TABLE IF NOT EXISTS raw_ozon_posting (
    id BIGSERIAL PRIMARY KEY,
    account TEXT,
    posting_number TEXT,
    scheme TEXT,                 -- fbs | fbo
    status TEXT,
    in_process_at TIMESTAMPTZ,
    period_from DATE,
    period_to DATE,
    payload JSONB NOT NULL,
    loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, posting_number)
);

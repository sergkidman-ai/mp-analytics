-- 019: ставки Ozon по SKU в кампаниях (ежедневный снимок для тренда + правка из дашборда).
-- bid хранится в рублях (Ozon отдаёт микрорубли, делим на 1e6). target_cir = целевой ДРР.
CREATE TABLE IF NOT EXISTS ozon_bids (
    account TEXT, campaign_id TEXT, campaign_title TEXT, adv_type TEXT,
    sku TEXT, title TEXT, bid NUMERIC, target_cir NUMERIC,
    captured_at DATE,
    PRIMARY KEY (account, campaign_id, sku, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_ozon_bids ON ozon_bids(account, captured_at);

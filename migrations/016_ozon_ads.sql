-- 016: реклама Ozon Performance по кампаниям (расход/ДРР).
-- pay_model: «Оплата за заказ» (ALL_SKU_PROMO/SEARCH_PROMO, % с заказа) vs «Трафареты» (SKU/баннер).
CREATE TABLE IF NOT EXISTS ozon_ads (
    account TEXT, period DATE, campaign_id TEXT,
    title TEXT, adv_type TEXT, pay_model TEXT, state TEXT,
    spend NUMERIC, views BIGINT, clicks BIGINT, ad_revenue NUMERIC, sold NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_ozon_ads_period ON ozon_ads(account, period);

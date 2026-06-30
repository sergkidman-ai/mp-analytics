-- 017: реклама WB «Продвижение» по кампаниям (расход/ДРР).
CREATE TABLE IF NOT EXISTS wb_ads (
    account TEXT, period DATE, advert_id BIGINT,
    name TEXT, adv_type INT, status INT,
    spend NUMERIC, views BIGINT, clicks BIGINT, orders BIGINT, revenue NUMERIC,
    ctr NUMERIC, cpc NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period, advert_id)
);
CREATE INDEX IF NOT EXISTS idx_wb_ads_period ON wb_ads(account, period);

-- 018: дневной расход на рекламу (для колонки «Реклама» в недельных отчётах).
-- Заполняется коллекторами ozon_ads (expense CSV по датам) и wb_ads (fullstats.days[]).
CREATE TABLE IF NOT EXISTS ad_spend_daily (
    account TEXT, platform TEXT, date DATE, spend NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, platform, date)
);
CREATE INDEX IF NOT EXISTS idx_ad_spend_daily ON ad_spend_daily(account, platform, date);

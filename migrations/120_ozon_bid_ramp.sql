-- поток: mkt
-- Плавный разгон ставок Ozon (+10 % в день до цели) и ежедневные снимки для наблюдения.

-- цель по каждой позиции «товар × кампания»: куда ведём ставку
CREATE TABLE IF NOT EXISTS mkt_ozon_bid_ramp (
    account       text        NOT NULL,
    campaign_id   text        NOT NULL,
    sku           text        NOT NULL,
    offer_id      text,
    bid_start     numeric     NOT NULL,          -- ставка на старте разгона
    bid_target    numeric     NOT NULL,          -- цель (35 % потолка)
    bid_current   numeric     NOT NULL,          -- что стоит сейчас по нашим данным
    step_pct      numeric     NOT NULL DEFAULT 10,
    grp           text,                          -- A реклама тянет / B продаёт сама / C нет продаж в июле
    status        text        NOT NULL DEFAULT 'planned',  -- planned | running | done | paused
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, campaign_id, sku)
);

-- журнал шагов: что и когда меняли (и применили ли на площадке)
CREATE TABLE IF NOT EXISTS mkt_ozon_bid_step_log (
    step_date     date        NOT NULL,
    account       text        NOT NULL,
    campaign_id   text        NOT NULL,
    sku           text        NOT NULL,
    bid_before    numeric,
    bid_after     numeric,
    bid_target    numeric,
    applied       boolean     NOT NULL DEFAULT false,   -- false = только план (dry-run)
    api_response  text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (step_date, account, campaign_id, sku)
);

-- ежедневный снимок эффекта: расход/показы/заказы по позиции
CREATE TABLE IF NOT EXISTS mkt_ozon_ads_sku_daily (
    stat_date     date        NOT NULL,
    account       text        NOT NULL,
    campaign_id   text        NOT NULL,
    sku           text        NOT NULL,
    offer_id      text,
    bid           numeric,
    views         bigint,
    clicks        bigint,
    money_spent   numeric,
    orders_qty    numeric,
    orders_money  numeric,
    drr           numeric,
    collected_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stat_date, account, campaign_id, sku)
);
CREATE INDEX IF NOT EXISTS mkt_ozon_ads_sku_daily_sku_idx ON mkt_ozon_ads_sku_daily (account, sku, stat_date);

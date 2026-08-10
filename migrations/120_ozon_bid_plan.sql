-- поток: mkt
-- План изменения ставок Ozon: цель по каждому SKU + доказательная база.
-- Витрина «Ставки Ozon» показывает не «поднять до X одним движением», а лестницу:
-- следующая ставка = текущая из снимка × (1 + шаг), но не выше цели.
-- Поэтому таблица хранит ЦЕЛЬ и потолок, а текущая ставка всегда берётся из ozon_bids.
CREATE TABLE IF NOT EXISTS mkt_ozon_bid_plan (
    built_at        date        NOT NULL,
    account         text        NOT NULL,
    sku             text        NOT NULL,
    campaign_id     bigint      NOT NULL DEFAULT 0,
    campaign_title  text,
    offer_id        text,
    name            text,
    action          text        NOT NULL,   -- raise | cut | drop
    bid_at_plan     numeric,                -- ставка на момент построения плана
    bid_target      numeric,                -- цель (доля потолка)
    bid_ceiling     numeric,                -- потолок: клик, съедающий всю прибыль
    our_price       numeric,
    margin_pct      numeric,                -- маржа с учётом отключения допов
    cr              numeric,                -- конверсия в заказ
    qty90           integer,
    revenue90       numeric,
    search_pos      numeric,                -- средняя позиция в поиске (ozon_search_product)
    view_conv       numeric,                -- конверсия из показа в карточку
    reason          text,
    PRIMARY KEY (built_at, account, sku, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_ozon_bid_plan_acc
    ON mkt_ozon_bid_plan (account, built_at DESC, action);

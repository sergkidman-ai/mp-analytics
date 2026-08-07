-- поток: mkt
-- 117_ozon_query_econ.sql — витрина «фраза × SKU × позиция × маржа» (шаг 5 плана).
--
-- Зачем: ставка назначается не «по цене товара» (так нарезаны кампании сейчас), а по фразе:
-- платить имеет смысл там, где есть живой спрос, у SKU есть запас маржи и мы вне топа.
-- Сводит четыре источника, которые до этого жили порознь:
--   ozon_search_query        — позиция, спрос, заказы, GMV по паре (фраза, SKU);
--   mkt_ozon_margin_control  — цена, маржа-live, предел снижения (можно ли вообще разгонять);
--   ozon_bids                — платим ли мы сейчас за этот SKU и сколько;
--   имя товара               — грубая релевантность фразы (см. rel_kind / name_overlap).
--
-- ГРАБЛИ: `unique_search_users` — свойство ФРАЗЫ, а не пары. Одна и та же фраза приходит
-- строкой на каждый наш SKU, попавший в выдачу, поэтому суммировать спрос по строкам НЕЛЬЗЯ
-- (даёт кратное завышение). В агрегатах брать max() по фразе, а не sum().
--
-- Позиция дробная (средняя за период) и 0 = нас в выдаче по этой фразе не было.
CREATE TABLE IF NOT EXISTS mkt_ozon_query_econ (
    account         text        NOT NULL,
    period_start    date        NOT NULL,
    period_end      date        NOT NULL,
    query           text        NOT NULL,
    sku             text        NOT NULL,
    offer_id        text,
    name            text,
    -- спрос и результат по фразе
    position        numeric,            -- 0 = нас нет в выдаче
    demand          bigint,             -- уникальные искавшие; свойство ФРАЗЫ, не пары
    views           bigint,
    view_conv       numeric,
    orders          bigint,
    gmv             numeric,
    -- релевантность фразы нашему товару
    rel_kind        text,               -- расходник / техника / прочее
    query_kind      text,               -- модельная / широкая (одно общее слово без модели)
    name_overlap    numeric,            -- доля слов фразы, встретившихся в названии товара
    -- экономика SKU (снимок контроля маржи)
    our_price       numeric,
    margin_own_live numeric,
    discount_limit_pct numeric,
    color_index     text,
    verdict         text,
    -- платим ли за него сейчас
    in_campaign     boolean,
    bid             numeric,
    campaigns       integer,
    -- что делать
    action          text,
    built_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, period_start, query, sku)
);

CREATE INDEX IF NOT EXISTS idx_mozqe_action
    ON mkt_ozon_query_econ (account, period_start, action, demand DESC);
CREATE INDEX IF NOT EXISTS idx_mozqe_sku
    ON mkt_ozon_query_econ (account, sku, period_start DESC);

-- Добавлено при первом прогоне: без различения «широкая / модельная» верх списка ставок
-- занимают однословные запросы («набор», «комплект», «для», «samsung») — спрос там
-- десятки тысяч, а намерение нулевое, и Ozon показывает по ним десятки наших SKU сразу.
ALTER TABLE mkt_ozon_query_econ ADD COLUMN IF NOT EXISTS query_kind text;

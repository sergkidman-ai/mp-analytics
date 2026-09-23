-- поток: prc
-- 415_prc_mp_price_day.sql — ежедневный срез цены и участия в акциях по каждой продающейся
-- карточке, все три площадки и обе фирмы. Зачем: в отчётах площадок цена приходит УЖЕ со
-- скидкой, а величина скидки продавца и флаг акции — пустые. Без истории входов/выходов
-- нельзя ответить, окупается ли участие в акции (вопрос Сергея 23.09.2026 по коду 5422:
-- та же карточка в акции у Дисквэра и без акции у Цифрового).
-- Состав: только карточки с продажами за последние 90 дней (решение Сергея 23.09.2026).
CREATE TABLE IF NOT EXISTS prc_mp_price_day (
    platform    text NOT NULL,                  -- wb / ozon / yandex
    account     text NOT NULL,
    item_id     text NOT NULL,                  -- nm_id (WB) / sku (Ozon) / offer_id (Маркет)
    day         date NOT NULL,
    vendor_code text,                           -- наш артикул
    price_list  numeric(12,2),                  -- цена до скидки продавца
    price_sale  numeric(12,2),                  -- цена после скидки продавца (до СПП/скидки МП)
    price_buyer numeric(12,2),                  -- что платит покупатель (WB: с СПП, Ozon: marketing_price)
    discount_pct numeric(6,2),                  -- скидка продавца, %
    in_promo    boolean NOT NULL DEFAULT false,
    promo_names text,                           -- названия акций через «; »
    stock       int,
    source      text NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, account, item_id, day)
);
CREATE INDEX IF NOT EXISTS prc_mp_price_day_item_idx ON prc_mp_price_day (item_id, day);

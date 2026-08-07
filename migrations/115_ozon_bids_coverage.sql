-- поток: mkt
-- 115_ozon_bids_coverage.sql — снимок ставок Ozon перестаёт быть огрызком.
--
-- Что было не так (проверено 07.08 живыми запросами Performance API):
--   1. `GET /api/client/campaign/{id}/v2/products` БЕЗ параметров отдаёт ровно 30 строк —
--      это дефолтный размер страницы, а не весь состав кампании. В кампании 12704286
--      реально 8 093 товара, в 10626733 — 2 381. Мы клали по 30 и считали, что это всё.
--   2. Собирались только `CAMPAIGN_STATE_RUNNING` — на acc1 это 9 кампаний из 33.
--   3. Кампании типов SEARCH_PROMO / ALL_SKU_PROMO на `/v2/products` отвечают 400: у них
--      свой эндпоинт `POST /api/client/campaign/search_promo/v2/products`, и он отдаёт
--      НЕ состав кампании, а весь пул товаров аккаунта, доступных продвижению в поиске
--      (acc1: 3 302 SKU; выдача одинакова для любого campaignId, включая несуществующий).
--
-- Отсюда две правки схемы:
--   `ozon_bids` получает состояние кампании и цену товара — чтобы «много кампаний» было
--   видно как факт, а не как молчаливый фильтр внутри коллектора;
--   `ozon_search_promo` — отдельный снимок пула продвижения в поиске: он per-account,
--   в `ozon_bids` ему места нет. Оттуда же приходит индекс видимости — сырьё для шага 5.

-- Цены товара в составе SKU-кампании НЕТ: `/v2/products` отдаёт ровно четыре поля
-- (sku, bid, title, targetCir). Цена есть только в пуле продвижения в поиске (ниже)
-- и в наших витринах — join по sku, отдельной колонки в `ozon_bids` не заводим.
ALTER TABLE ozon_bids ADD COLUMN IF NOT EXISTS state text;   -- состояние КАМПАНИИ

-- Та же болезнь у `ozon_ads`, только тише: строки июля собраны 17.07 и покрывают 01–16.07,
-- то есть половину месяца, а выглядят как месяц. Отсюда `covered_to` — до какого дня период
-- реально закрыт; без него «расход за июль 410 867 ₽» неотличим от полного (по факту 711 089 ₽).
-- Плюс `orders` — заказы с продвижения, их отдаёт /api/client/statistics/daily/json.
ALTER TABLE ozon_ads ADD COLUMN IF NOT EXISTS orders     numeric;
ALTER TABLE ozon_ads ADD COLUMN IF NOT EXISTS covered_to date;

CREATE TABLE IF NOT EXISTS ozon_search_promo (
    account            text NOT NULL,
    sku                text NOT NULL,
    captured_at        date NOT NULL,
    source_sku         text,          -- sourceSku = наш код товара (4 цифры), мост к каталогу
    title              text,
    price              numeric,
    bid                numeric,       -- ставка, ₽ (в API — микрорубли)
    bid_without_additive numeric,
    carrots_additive   numeric,
    views_week         bigint,        -- views = {thisWeek, previousWeek}, а не число
    views_prev_week    bigint,
    -- индекс видимости приходит СТРОКОЙ и бывает «10+» — это ведро, а не число: храним как есть
    visibility_index   text,
    prev_visibility_index text,
    promo_status       boolean,       -- searchPromoStatus: участвует ли товар сейчас
    available          boolean,       -- isSearchPromoAvailable
    carrots_status     text,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, sku, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_ozsp_day ON ozon_search_promo (captured_at, account);
CREATE INDEX IF NOT EXISTS idx_ozbids_day_state ON ozon_bids (captured_at, account, state);

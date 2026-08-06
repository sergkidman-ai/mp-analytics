-- 109 (mkt): поисковые запросы Ozon по товарам — недельная история.
-- Источник: Seller API /v1/analytics/product-queries (сводка по SKU)
--           и /v1/analytics/product-queries/details (конкретные фразы).
-- Доступ: работает на ОБЕИХ подписках (проверено A/B 30.07.2026, docs/ozon-recon-report.md).
--
-- Период — полуоткрытый интервал [period_start, period_end): date_to у Ozon ИСКЛЮЧАЮЩИЙ
-- (проверено: date_to=19.07 и 20.07 дают одинаковые цифры за 13–19.07). Неделя = Пн..Пн.
-- Глубина у Ozon ~6 недель, дальше 400 «There is no data for the specified period» —
-- поэтому история копится только здесь, у себя.
--
-- ЧЕГО В API НЕТ: кликов и добавлений в корзину по запросу Ozon не отдаёт вообще.
-- Ближайшее, что есть: unique_search_users (искали) → unique_view_users (показали) →
-- order_count (заказали). Корзину можно взять только по SKU целиком из /v1/analytics/data
-- (hits_tocart_search), к тексту запроса она не привязывается.

-- Уровень запроса: одна строка = (аккаунт, неделя, sku, фраза).
CREATE TABLE IF NOT EXISTS ozon_search_query (
    account TEXT,
    period_start DATE,          -- понедельник, включительно
    period_end DATE,            -- следующий понедельник, ИСКЛЮЧИТЕЛЬНО
    sku TEXT,
    query TEXT,                 -- поисковая фраза покупателя
    position NUMERIC,           -- средняя позиция в выдаче; 0 = товар по фразе не показывался
    unique_search_users INTEGER,-- уникальных, кто искал эту фразу
    unique_view_users INTEGER,  -- уникальных, кому показали наш товар по этой фразе (= показы)
    view_conversion NUMERIC,    -- конверсия поиск → показ
    order_count INTEGER,        -- заказов по этой фразе
    gmv NUMERIC,
    currency TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period_start, sku, query)
);
CREATE INDEX IF NOT EXISTS idx_ozon_sq_period ON ozon_search_query(account, period_start);
CREATE INDEX IF NOT EXISTS idx_ozon_sq_query ON ozon_search_query(query);
CREATE INDEX IF NOT EXISTS idx_ozon_sq_sku ON ozon_search_query(account, sku);

-- Уровень товара: сводка за неделю по всем SKU (в т.ч. тем, у кого фраз не набралось).
CREATE TABLE IF NOT EXISTS ozon_search_product (
    account TEXT,
    period_start DATE,
    period_end DATE,
    sku TEXT,
    offer_id TEXT,
    name TEXT,
    category TEXT,
    position NUMERIC,
    unique_search_users INTEGER,
    unique_view_users INTEGER,
    view_conversion NUMERIC,
    order_count INTEGER,
    gmv NUMERIC,
    currency TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period_start, sku)
);
CREATE INDEX IF NOT EXISTS idx_ozon_sp_period ON ozon_search_product(account, period_start);

-- Журнал прогонов: что за неделю уже забрано — чтобы cron дозаписывал только новое.
CREATE TABLE IF NOT EXISTS ozon_search_run (
    account TEXT,
    period_start DATE,
    period_end DATE,
    skus_total INTEGER,         -- сколько SKU отправлено в сводку
    skus_with_data INTEGER,     -- сколько вернулось со сводкой
    skus_detailed INTEGER,      -- по скольким брали фразы (прошли порог MIN_SEARCH)
    queries_rows INTEGER,       -- строк фраз ПОЛУЧЕНО в этом прогоне (не итог по таблице:
                                -- повторный забор той же недели присылает свой срез, часть
                                -- строк совпадает с уже накопленными — см. ozon_search_query)
    api_calls INTEGER,
    tail_dropped INTEGER,       -- сторож целостности: недобрано строк против total (в норме 0)
    finished_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period_start)
);

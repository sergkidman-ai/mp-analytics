-- поток: mkt
-- 112_ozon_price_index.sql — ценовые индексы Ozon по каждому товару, снимками на дату.
--
-- Источник: POST /v5/product/info/prices (см. collectors/ozon_price_index.py).
-- Зачем: буст в поиске работает только у товаров с выгодным индексом цен; по разведке
-- 28.07.2026 около половины ассортимента сидит в «красной» зоне и подписку не отрабатывает
-- (docs/ozon-recon-report.md, разделы 4.1 и 9.3).
--
-- Храним СНИМКАМИ: ключ (account, sku, collected_on). Индекс двигается вслед за конкурентом,
-- и без истории не отличить «мы подняли цену» от «рынок опустил свою». Повторный прогон
-- в тот же день перезаписывает снимок этого дня (идемпотентность, правило 3 CLAUDE.md).

CREATE TABLE IF NOT EXISTS ozon_price_index (
    account TEXT,
    sku TEXT,                       -- product_id Ozon
    offer_id TEXT,                  -- наш артикул
    collected_on DATE,              -- дата снимка

    -- цены, как их отдаёт Ozon (строками в API → NUMERIC здесь)
    price NUMERIC,                  -- текущая цена продажи
    old_price NUMERIC,              -- зачёркнутая
    marketing_price NUMERIC,        -- цена с учётом акций Ozon
    marketing_seller_price NUMERIC, -- цена с учётом наших акций
    min_price NUMERIC,              -- НАША установленная минимальная цена (не рыночная!)
    currency TEXT,
    auto_action_enabled BOOLEAN,    -- автоучастие в акциях Ozon

    -- зона индекса: строка от Ozon как есть (наблюдались SUPER / GREEN / YELLOW / RED /
    -- WITHOUT_INDEX). Намеренно НЕ мапим в свой энум: перечень значений меняется на стороне
    -- площадки, а сырьё должно переживать переименования (правило 2 CLAUDE.md).
    color_index TEXT,

    -- три индекса. *_min_price — фактическая минимальная цена РЫНКА, то есть цена конкурента
    -- в рублях. Это самое ценное в методе: прямой конкурентный сигнал по каждому SKU.
    external_min_price NUMERIC,     -- против цен на других площадках
    external_index NUMERIC,
    ozon_min_price NUMERIC,         -- против других продавцов того же товара на Ozon
    ozon_index NUMERIC,
    self_min_price NUMERIC,         -- против наших же цен на других маркетплейсах
    self_index NUMERIC,

    -- экономика продажи из того же вызова
    commission_fbo_pct NUMERIC,
    commission_fbs_pct NUMERIC,
    acquiring NUMERIC,
    volume_weight NUMERIC,

    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, sku, collected_on)
);

CREATE INDEX IF NOT EXISTS idx_ozon_pi_day   ON ozon_price_index(account, collected_on);
CREATE INDEX IF NOT EXISTS idx_ozon_pi_color ON ozon_price_index(account, collected_on, color_index);
CREATE INDEX IF NOT EXISTS idx_ozon_pi_offer ON ozon_price_index(account, offer_id);

-- Журнал прогонов: сколько карточек прошло, какое распределение зон получилось.
-- Нужен, чтобы отличить «красная зона выросла» от «прогон недокачал половину ассортимента».
CREATE TABLE IF NOT EXISTS ozon_price_index_run (
    account TEXT,
    collected_on DATE,
    items INTEGER,                  -- сколько карточек записали
    with_external INTEGER,          -- у скольких есть внешний индекс (по нему и судим зону)
    zones JSONB,                    -- {"RED": 2634, "GREEN": 1395, ...} — как отдал Ozon
    api_calls INTEGER,
    finished_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, collected_on)
);

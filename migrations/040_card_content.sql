-- 040: сырьё контента карточек площадок (описание + характеристики), которого раньше в БД
-- не было (wb_cards/ozon_product несут только заголовок+габариты). Нужно, чтобы grounding
-- отвечал по РОДНОЙ карточке, которую видит покупатель (чип/ресурс/совместимость), а не
-- только по описаниям МойСклада. Сырой ответ API целиком в payload; разбор — в grounding.

-- WB: content-api /content/v2/get/cards/list возвращает полный объект карточки, включая
-- description и characteristics[]. Кладём объект целиком.
CREATE TABLE IF NOT EXISTS raw_wb_card_content (
    account      text   NOT NULL,
    nm_id        bigint NOT NULL,
    vendor_code  text,
    payload      jsonb  NOT NULL,       -- полная карточка: description + characteristics[]
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, nm_id)
);

-- Ozon: /v4/product/info/attributes — структурные атрибуты (совместимые модели, тип, ресурс,
-- аннотация). Ключ склейки с площадкой у нас = offer_id (= МС code).
CREATE TABLE IF NOT EXISTS raw_ozon_attributes (
    account      text NOT NULL,
    offer_id     text NOT NULL,
    sku          text,
    payload      jsonb NOT NULL,        -- info/attributes: attributes[] + описание
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, offer_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_ozon_attr_sku ON raw_ozon_attributes (sku);

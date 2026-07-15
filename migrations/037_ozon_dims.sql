-- 037: задекларированные габариты карточек Ozon (для расчёта переплаты по объёмной логистике).
-- Источник — Ozon Seller API /v4/product/info/attributes (ДхШхВ мм, вес г). Ozon берёт
-- логистику по объёмному весу (= объём_см3/5000, кг), поэтому раздутый короб = переплата,
-- ровно как на WB. volume_l приведён к литрам; связка с продажами — по sku/offer_id.
CREATE TABLE IF NOT EXISTS ozon_dims (
    account     text    NOT NULL,
    sku         text    NOT NULL,
    offer_id    text,
    barcode     text,
    product_id  bigint,
    depth_mm    numeric,
    width_mm    numeric,
    height_mm   numeric,
    weight_g    numeric,
    volume_l    numeric,             -- ДхШхВ, приведён к литрам
    name        text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, sku)
);
CREATE INDEX IF NOT EXISTS idx_ozon_dims_offer ON ozon_dims (account, offer_id);

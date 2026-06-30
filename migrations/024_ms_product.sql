-- Полный справочник товаров МойСклад (ежедневный прайс поставщиков = buy_price).
-- Источник для себестоимости (замещающей), вкладки «Поставщики», предикций закупок/дефицита.
CREATE TABLE IF NOT EXISTS ms_product (
    ms_id         TEXT PRIMARY KEY,
    name          TEXT,
    article       TEXT,
    code          TEXT,
    external_code TEXT,
    buy_price     NUMERIC,     -- закупочная (прайс поставщика), ₽
    sale_price    NUMERIC,     -- цена продажи, ₽
    archived      BOOLEAN,
    updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ms_product_ext ON ms_product (external_code);
-- баркод -> ms_id (у товара бывает несколько баркодов; ключ стыковки с WB)
CREATE TABLE IF NOT EXISTS ms_barcode (
    barcode TEXT PRIMARY KEY,
    ms_id   TEXT
);

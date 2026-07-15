-- 036: габариты поставщиков (закупочная упаковка) для пересчёта логистики WB на факт.
-- Источник — ручные выгрузки прайсов/каталогов поставщиков (incoming/size/): Изи (T2),
-- Cactus, Сакура, Профилайн. Ключ связки с нашим каталогом — артикул (= products.article).
-- Объём и вес приведены к единым единицам: volume_l (литры), weight_kg (кг).
CREATE TABLE IF NOT EXISTS supplier_dims (
    supplier   text    NOT NULL,         -- изи | cactus | sakura | profiline
    article    text    NOT NULL,         -- артикул поставщика (= products.article)
    barcode    text,
    length_cm  numeric,
    width_cm   numeric,
    height_cm  numeric,
    weight_kg  numeric,
    volume_l   numeric,                  -- приведён к литрам
    title      text,
    src_file   text,
    loaded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (supplier, article)
);
CREATE INDEX IF NOT EXISTS idx_supplier_dims_article ON supplier_dims (article);
CREATE INDEX IF NOT EXISTS idx_supplier_dims_barcode ON supplier_dims (barcode);

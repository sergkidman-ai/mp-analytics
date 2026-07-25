-- поток: rev
-- Распродажа остатков WB: загруженный список (товар/цена/остатки на момент заливки).
-- Источник — xlsx из dropbox (по аккаунту). Живой остаток на складе WB берём из wb_stocks
-- (последний снимок), сигнал «поднять цену» = когда живой остаток стал 0.
CREATE TABLE IF NOT EXISTS wb_clearance (
    account            TEXT NOT NULL,
    nm_id              BIGINT NOT NULL,        -- Артикул WB (ключ джойна к wb_stocks)
    vendor_code        TEXT,                   -- Артикул продавца
    barcode            TEXT,                   -- Последний баркод
    brand              TEXT,
    category           TEXT,
    orig_price         NUMERIC,                -- Текущая цена (до скидки)
    discount_pct       NUMERIC,                -- Новая скидка, %
    clearance_price    NUMERIC,                -- Цена со скидкой
    uploaded_wb_stock  NUMERIC,               -- Остатки WB на момент загрузки (базлайн для «продано»)
    seller_stock       NUMERIC,               -- Остатки продавца (наш склад / FBS)
    source_file        TEXT,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, nm_id)
);
CREATE INDEX IF NOT EXISTS idx_wb_clearance_nm ON wb_clearance (nm_id);

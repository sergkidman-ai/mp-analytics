-- migrations/003_wb_stocks.sql — остатки на складах WB (FBO по данным WB) + возвраты в пути.
-- Снимок по дням: что и сколько лежит на складах WB, по nm_id и складу.
CREATE TABLE IF NOT EXISTS wb_stocks (
    account TEXT NOT NULL,
    nm_id BIGINT,
    vendor_code TEXT,
    warehouse TEXT,
    quantity NUMERIC,              -- доступно к продаже
    quantity_full NUMERIC,         -- всего на складе
    in_way_to_client NUMERIC,      -- в пути к клиенту
    in_way_from_client NUMERIC,    -- возвраты в пути обратно
    brand TEXT, subject TEXT,
    captured_at DATE NOT NULL,
    PRIMARY KEY(account, nm_id, warehouse, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_wb_stocks_nm ON wb_stocks(account, nm_id, captured_at);

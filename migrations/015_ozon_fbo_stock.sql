-- 015: остатки Ozon FBO по складам и аккаунтам (Ozon API v2 stock_on_warehouses).
-- Чтобы видеть ФБО раздельно по юрлицам (Цифровой/Дисквэр), как ВБ ФБО из wb_stocks.
CREATE TABLE IF NOT EXISTS ozon_fbo_stock (
    account TEXT, sku TEXT, warehouse TEXT,
    item_code TEXT, item_name TEXT,
    free_to_sell NUMERIC, reserved NUMERIC, promised NUMERIC,
    captured_at DATE,
    PRIMARY KEY (account, sku, warehouse, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_ozon_fbo_cap ON ozon_fbo_stock(account, captured_at);

-- migrations/009_supplier_stock.sql — ежедневный снимок остатков+поставщиков из МС
-- для дашборда дефицита (что кончается, что выкупать). stock_days — дни запаса (МС считает сам).
CREATE TABLE IF NOT EXISTS supplier_stock (
    captured_at DATE,
    ms_id TEXT,
    name TEXT,
    article TEXT,
    external_code TEXT,
    supplier TEXT,
    buy_price NUMERIC,      -- закупочная цена
    cost_seb NUMERIC,       -- себестоимость (report/stock price)
    stock NUMERIC,
    in_transit NUMERIC,
    reserve NUMERIC,
    stock_days NUMERIC,     -- дни до конца запаса (МС)
    PRIMARY KEY (captured_at, ms_id)
);
CREATE INDEX IF NOT EXISTS idx_supstock_days ON supplier_stock(captured_at, stock_days);

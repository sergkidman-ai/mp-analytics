-- 010: остатки ПО СКЛАДАМ (наш / удалённый поставщика / брак раздельно).
DROP TABLE IF EXISTS supplier_stock;
CREATE TABLE supplier_stock (
    captured_at DATE, ms_id TEXT, store TEXT,
    name TEXT, article TEXT, external_code TEXT, supplier TEXT,
    buy_price NUMERIC, cost_seb NUMERIC,
    stock NUMERIC, in_transit NUMERIC, reserve NUMERIC, stock_days NUMERIC,
    PRIMARY KEY (captured_at, ms_id, store)
);
CREATE INDEX idx_supstock ON supplier_stock(captured_at, store, stock_days);

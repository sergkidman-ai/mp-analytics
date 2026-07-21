-- 045: сигналы оборачиваемости Ozon FBO по SKU (Ozon API v1 analytics/stocks) — снимок на дату.
-- Основной сигнал к вывозу со склада — days_without_sales (per-SKU «платного хранения»
-- Ozon API не отдаёт, проверено; excess_stock_count по картриджам всегда 0). Точный склад и
-- количество для заявки берём из ozon_fbo_stock, здесь — только сигналы, join по (account, sku).
CREATE TABLE IF NOT EXISTS ozon_stock_signals (
    account TEXT,
    sku TEXT,
    offer_id TEXT,
    name TEXT,
    days_without_sales INTEGER,     -- max по кластерам (самый застойный)
    turnover_grade TEXT,            -- грейды через запятую (обычно NO_SALES)
    excess_stock_count INTEGER,     -- сумма по кластерам
    ads NUMERIC,                    -- среднесуточные продажи, max
    idc NUMERIC,                    -- дни покрытия
    captured_at DATE,
    PRIMARY KEY (account, sku, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_ozon_signals_cap ON ozon_stock_signals(account, captured_at);

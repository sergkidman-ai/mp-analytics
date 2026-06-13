-- migrations/001_init.sql — схема БД (Этап 0).
-- Таблицы строго по разделу 5 ARCHITECTURE.md (raw → clean → marts). Без отсебятины.

-- ============================================================
-- Слой 1 — RAW (сырьё как пришло из API)
-- ============================================================
CREATE TABLE raw_moysklad_product (
    id BIGSERIAL PRIMARY KEY, ms_id TEXT, article TEXT,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(ms_id)
);
CREATE TABLE raw_wb_report (
    id BIGSERIAL PRIMARY KEY, account TEXT, rrd_id BIGINT,
    period_from DATE, period_to DATE,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, rrd_id)
);
CREATE TABLE raw_ozon_transaction (
    id BIGSERIAL PRIMARY KEY, account TEXT, operation_id BIGINT,
    period_from DATE, period_to DATE,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, operation_id)
);
CREATE TABLE raw_yandex_order (
    id BIGSERIAL PRIMARY KEY, account TEXT, order_id BIGINT, item_id BIGINT,
    period_from DATE, period_to DATE,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, order_id, item_id)
);
CREATE TABLE raw_positions (
    id BIGSERIAL PRIMARY KEY, platform TEXT, account TEXT,
    article TEXT, keyword TEXT, position INT, captured_at DATE,
    payload JSONB, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(platform, account, article, keyword, captured_at)
);

-- ============================================================
-- Слой 2 — CLEAN (единый вид)
-- ============================================================
-- Справочник товаров (из МойСклад — источник правды)
CREATE TABLE products (
    article TEXT PRIMARY KEY,         -- единый артикул (code/article)
    ms_id TEXT, title TEXT, category TEXT,
    buy_price NUMERIC,                -- себестоимость за единицу (из МойСклад)
    length_cm NUMERIC, width_cm NUMERIC, height_cm NUMERIC,
    weight_kg NUMERIC, volume_l NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE stocks (
    article TEXT, source TEXT,        -- moysklad|wb|ozon|yandex
    account TEXT, qty NUMERIC, captured_at DATE,
    PRIMARY KEY(article, source, account, captured_at)
);

CREATE TABLE sales (
    id BIGSERIAL PRIMARY KEY,
    article TEXT NOT NULL, platform TEXT NOT NULL, account TEXT NOT NULL,
    period_from DATE NOT NULL, period_to DATE NOT NULL,
    granularity TEXT NOT NULL,        -- day|week|month
    qty NUMERIC DEFAULT 0,
    our_price NUMERIC, buyer_price NUMERIC,
    revenue_buyer NUMERIC DEFAULT 0, to_pay NUMERIC DEFAULT 0,
    commission NUMERIC DEFAULT 0, logistics NUMERIC DEFAULT 0,
    logistics_cnt NUMERIC DEFAULT 0, returns_sum NUMERIC DEFAULT 0,
    storage NUMERIC DEFAULT 0, acceptance NUMERIC DEFAULT 0, other NUMERIC DEFAULT 0,
    loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(article, platform, account, period_from, period_to, granularity)
);
CREATE INDEX idx_sales_article ON sales(article);
CREATE INDEX idx_sales_period ON sales(period_from, period_to);

CREATE TABLE funnel (
    article TEXT, platform TEXT, account TEXT,
    period_from DATE, period_to DATE, granularity TEXT,
    views NUMERIC, clicks NUMERIC, add_to_cart NUMERIC,
    orders NUMERIC, open_card NUMERIC, cr NUMERIC,
    PRIMARY KEY(article, platform, account, period_from, period_to, granularity)
);

CREATE TABLE ads (
    article TEXT, platform TEXT, account TEXT,
    campaign TEXT, period_from DATE, period_to DATE,
    views NUMERIC, clicks NUMERIC, spend NUMERIC,
    orders NUMERIC, revenue NUMERIC, drr NUMERIC,
    PRIMARY KEY(article, platform, account, campaign, period_from, period_to)
);

-- ============================================================
-- Слой 3 — MARTS (витрины, пересчитываются)
-- ============================================================
CREATE TABLE margin_by_sku (
    article TEXT, platform TEXT, account TEXT,
    period_from DATE, period_to DATE,
    qty NUMERIC, revenue_buyer NUMERIC, cogs NUMERIC,
    commission NUMERIC, logistics NUMERIC, returns_sum NUMERIC,
    storage NUMERIC, acceptance NUMERIC, other NUMERIC,
    net_profit NUMERIC, margin_pct NUMERIC,
    spp_pct NUMERIC, commission_pct NUMERIC,
    computed_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY(article, platform, account, period_from, period_to)
);

CREATE TABLE drops (
    article TEXT, platform TEXT, account TEXT,
    metric TEXT,                      -- revenue|orders|views|cr
    horizon TEXT,                     -- day|week|year
    current_val NUMERIC, prev_val NUMERIC, change_pct NUMERIC,
    likely_cause TEXT,
    detected_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY(article, platform, account, metric, horizon, detected_at)
);

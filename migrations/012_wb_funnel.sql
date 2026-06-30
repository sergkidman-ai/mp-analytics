-- 012: воронка продаж WB (трафик/клики/конверсии/рейтинг карточки за месяц).
-- Источник: seller-analytics-api /api/analytics/v3/sales-funnel/products.
-- period = первое число месяца (ключ как в margin_by_sku). past_* = прошлый период из ответа WB.
CREATE TABLE IF NOT EXISTS wb_funnel (
    account TEXT, period DATE, nm_id BIGINT,
    title TEXT, vendor_code TEXT, brand TEXT, subject_name TEXT,
    product_rating NUMERIC, feedback_rating NUMERIC,
    open_count INT, cart_count INT, order_count INT, order_sum NUMERIC,
    buyout_count INT, buyout_sum NUMERIC, cancel_count INT, cancel_sum NUMERIC,
    add_to_cart_pct NUMERIC, cart_to_order_pct NUMERIC, buyout_pct NUMERIC,
    share_order_pct NUMERIC, stock_wb INT, stock_mp INT,
    past_open_count INT, past_order_sum NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, period, nm_id)
);
CREATE INDEX IF NOT EXISTS idx_wb_funnel_period ON wb_funnel(account, period);

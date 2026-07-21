-- 101_wb_market_price.sql — реальная цена покупателя (после АКЦИИ) из публичного card.wb.ru v4.
-- Prices API (wb_price.discounted_price) даёт цену БЕЗ акции → завышает. market_price = что видит покупатель.
ALTER TABLE wb_price ADD COLUMN IF NOT EXISTS market_price        numeric;      -- product из v4 (после акции, до личной СПП)
ALTER TABLE wb_price ADD COLUMN IF NOT EXISTS market_basic        numeric;      -- basic из v4 (до скидки)
ALTER TABLE wb_price ADD COLUMN IF NOT EXISTS market_captured_at  timestamptz;

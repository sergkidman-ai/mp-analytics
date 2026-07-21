-- 105_mkt_margin_own.sql — маржа от НАШЕЙ цены до акций (KPI ≥25%) + 25%-лимит акции.
-- Главный KPI бизнеса: чистая ≥25% от price_before_promo (list, до акций/СПП), НЕ от реализации.
--   margin_pct_own = net_u / price_before_promo × 100        (форвард; целевой порог 25%)
--   promo_limit_25 = глубина акции, при которой margin_own падает до 25%:
--     d = 1 − (0.25 + (лог+хран+приёмка+COGS)/list) / payout   ← guardrail для решений по акциям

ALTER TABLE mkt_sku_economics
  ADD COLUMN IF NOT EXISTS margin_pct_own numeric,  -- net_u / price_before_promo (маржа от нашей цены до акций, KPI)
  ADD COLUMN IF NOT EXISTS promo_limit_25 numeric;  -- глубина акции, где margin_own=25% (доля 0..1; <0 = уже ниже 25%)

COMMENT ON COLUMN mkt_sku_economics.margin_pct_own IS 'маржа от НАШЕЙ цены до акций (net/price_before_promo), KPI ≥25%';
COMMENT ON COLUMN mkt_sku_economics.margin_pct_wb  IS 'маржа от цены РЕАЛИЗАЦИИ после СПП (net/buyer_price), справочно';
COMMENT ON COLUMN mkt_sku_economics.promo_limit_25 IS 'макс. глубина акции, при которой ещё держим KPI 25% от list';

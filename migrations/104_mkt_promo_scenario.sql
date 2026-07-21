-- 104_mkt_promo_scenario.sql — сценарий «маржа vs глубина акции» в mkt_sku_economics.
-- Глубина акции — НАШ рычаг (роняет базу → payout → net), в отличие от СПП (её несёт ВБ).
-- Для каждого SKU считаем net/маржу при разной глубине акции + точку безубытка.
--   база(d)   = price_before_promo × (1 − d)
--   to_pay(d) = база(d) × payout_ratio
--   net(d)    = to_pay(d) − логистика − хранение − приёмка − COGS
--   breakeven = глубина d, при которой net=0 (максимально допустимая скидка до убытка)

ALTER TABLE mkt_sku_economics
  ADD COLUMN IF NOT EXISTS scenario_promo      jsonb,    -- [{promo_pct, base, buyer_u, to_pay_u, net_u, margin_pct}, ...]
  ADD COLUMN IF NOT EXISTS promo_breakeven_pct numeric;  -- глубина акции при net=0 (доля 0..1; <0 = убыток даже без акции)

COMMENT ON COLUMN mkt_sku_economics.scenario_promo IS 'сценарий маржи по глубине акции (сетка + текущая глубина)';
COMMENT ON COLUMN mkt_sku_economics.promo_breakeven_pct IS 'макс. глубина акции до net=0: 1 − (лог+хран+приёмка+COGS)/(payout×list)';

-- 103_mkt_payout_ratio.sql — форвард mkt_sku_economics через payout-ratio вместо модели комиссии.
-- Открытие (проверено 4913 продаж acc1): наш payout = ppvz_for_pay/база ≈ 63% и от СПП НЕ зависит
-- (СПП гасится комиссией ВБ 1:1). Поэтому net форвардим НЕ через commission%×promo, а через
-- стабильный per-SKU payout из трейлинг-факта:
--   to_pay_u = promo_price × payout_ratio ;  net_u = to_pay_u − логистика − приёмка − COGS
-- promo_price (база) — уже per-SKU (list × личная глубина акции), payout_ratio — per-SKU трейлинг (фолбэк медиана).

ALTER TABLE mkt_sku_economics
  ADD COLUMN IF NOT EXISTS payout_ratio     numeric,  -- к_перечислению/база (ppvz_for_pay/withdisc), СПП-независим
  ADD COLUMN IF NOT EXISTS payout_source    text,     -- 'sku' (трейлинг этого SKU) | 'median' (фолбэк)
  ADD COLUMN IF NOT EXISTS to_pay_u         numeric,  -- форвард: promo_price × payout_ratio (после комиссии, ДО логистики)
  -- трейлинг-факт (окно N дней) — «ground truth», рядом с форвардом
  ADD COLUMN IF NOT EXISTS trail_days       integer,  -- ширина окна, дней
  ADD COLUMN IF NOT EXISTS trail_qty        numeric,  -- продано штук в окне
  ADD COLUMN IF NOT EXISTS trail_realized_u numeric,  -- средняя реализация (после СПП)/шт в окне
  ADD COLUMN IF NOT EXISTS trail_spp_pct    numeric;  -- средняя СПП в окне (1 − realized/base)

COMMENT ON COLUMN mkt_sku_economics.payout_ratio IS 'ppvz_for_pay/база; ≈0.63, от СПП не зависит (комиссия гасит СПП)';
COMMENT ON COLUMN mkt_sku_economics.commission_u IS 'полное удержание ВБ из базы = promo_price×(1−payout) (комиссия−СПП-компенсация+эквайринг)';
COMMENT ON COLUMN mkt_sku_economics.commission_pct IS '1 − payout_ratio (доля базы, удержанная ВБ)';

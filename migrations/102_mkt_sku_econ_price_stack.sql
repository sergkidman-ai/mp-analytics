-- 102_mkt_sku_econ_price_stack.sql — явный 3-ценовой стек WB в витрине mkt_sku_economics.
-- Раньше цена лежала одним размытым полем price_card (то до СПП, то после). Теперь три цены
-- хранятся отдельно, все видны напрямую из 2 API (Prices API + публичный v4):
--   2671 (before_promo) ──промо 13%──▶ 2324 (promo_price) ──СПП 20%──▶ 1859 (buyer_price)
-- Проверка на 216421567 (2026-07-20): 2671×0.87×0.80 = 1858.6 ≈ 1859.

ALTER TABLE mkt_sku_economics
  -- 3-ценовой стек (текущая карточка)
  ADD COLUMN IF NOT EXISTS price_before_promo numeric,  -- до акции: v4 basic = Prices API price. Пример 2671
  ADD COLUMN IF NOT EXISTS promo_price        numeric,  -- акционная (после промо, ДО СПП) = Prices API discountedPrice.
                                                         -- Пример 2324. ЭТО БАЗА, от которой ВБ считает комиссию и СПП.
  ADD COLUMN IF NOT EXISTS buyer_price        numeric,  -- цена покупателя (после СПП) = v4 product = revenue_wb.
                                                         -- Пример 1859. ЗНАМЕНАТЕЛЬ маржи%ВБ.
  ADD COLUMN IF NOT EXISTS promo_pct          numeric,  -- % акции (Prices API discount), доля 0..1
  ADD COLUMN IF NOT EXISTS spp_pct_card       numeric,  -- СПП текущей карточки = 1 − buyer_price/promo_price
  -- сигнал «продаётся ли по текущей цене» (форвард-маржа врёт, если цену подняли и продажи встали)
  ADD COLUMN IF NOT EXISTS last_sale_date     date,     -- послед. продажа: max rr_dt (doc=Продажа, qty>0)
  ADD COLUMN IF NOT EXISTS days_since_sale    integer;  -- дней с последней продажи на момент сборки

COMMENT ON COLUMN mkt_sku_economics.promo_price IS 'акционная цена (после промо, до СПП) — база комиссии/СПП';
COMMENT ON COLUMN mkt_sku_economics.buyer_price IS 'цена покупателя после СПП (v4 product) = revenue_wb, знаменатель маржи';
COMMENT ON COLUMN mkt_sku_economics.price_card  IS 'DEPRECATED: дублирует buyer_price; оставлено для совместимости';

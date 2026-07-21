-- 106_mkt_margin_own_actual.sql — фактическая маржа месяца от НАШЕЙ промо-цены.
-- margin_pct_own_actual = 100 × прибыль_месяца / выручка_до_СПП(revenue_buyer) = net_profit/revenue_buyer.
-- Знаменатель = промо-цена, что мы задаём в акцию (до СПП), за тот же месяц (period_econ).
-- qty у наборов раздут компонентами, но в отношении ÷qty сокращается → корректно и для наборов.
-- Колонка «Маржа <месяц>» на странице маркетинга берёт это поле напрямую.

ALTER TABLE mkt_sku_economics
  ADD COLUMN IF NOT EXISTS margin_pct_own_actual numeric;

COMMENT ON COLUMN mkt_sku_economics.margin_pct_own_actual IS
  'факт маржа месяца от НАШЕЙ промо-цены (net_profit/revenue_buyer), период = period_econ';

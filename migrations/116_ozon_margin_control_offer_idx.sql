-- поток: mkt
-- 116_ozon_margin_control_offer_idx.sql — индекс под мост «ставка → витрина маржи».
--
-- Ставки ведутся по sku, витрина маржи — по offer_id, мост между ними — `ozon_product`.
-- Пока ставок было 209, мост работал подзапросом `mc.offer_id IN (SELECT …)` и этого никто
-- не замечал; на 14 035 строках снимка тот же запрос стал занимать 35 секунд: PK витрины
-- начинается с `captured_date`, поэтому поиск по одному offer_id шёл сканом.
CREATE INDEX IF NOT EXISTS idx_mozmc_offer
    ON mkt_ozon_margin_control (account, offer_id, captured_date DESC);

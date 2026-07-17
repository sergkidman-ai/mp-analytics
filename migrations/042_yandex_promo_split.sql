-- 042: реклама Яндекс.Маркета — раздельно (Fix 3 сверки с «Отчётом о стоимости услуг»).
-- Раньше буст продаж + буст показов + Полки + баннеры схлопывались в один promotion.
-- Теперь источаем из raw_yandex_services раздельными категориями:
--   boost_sales — буст продаж (оплата за продажи, boost.csv: PREPAID+POSTPAID);
--   boost_shows — буст показов + товарные баннеры (cpm-boost/product-banners.csv: PAYMENT);
--   shelf       — Полки (shelf.csv / лист «Полки»).
-- promotion остаётся агрегатом (Σ трёх) для совместимости витрины.
-- Параллельно комиссия/логистика/эквайринг/прочее переисточены из того же отчёта
-- (колонки fee/delivery/transfer/other_fee — тип не меняется, меняется лишь источник в коллекторе).

ALTER TABLE yandex_finance_monthly ADD COLUMN IF NOT EXISTS boost_sales numeric;
ALTER TABLE yandex_finance_monthly ADD COLUMN IF NOT EXISTS boost_shows numeric;
ALTER TABLE yandex_finance_monthly ADD COLUMN IF NOT EXISTS shelf       numeric;

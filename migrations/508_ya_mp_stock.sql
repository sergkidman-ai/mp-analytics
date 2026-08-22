-- поток: ev — остатки на складах Маркета по ВСЕМ активным магазинам, не только FBY.
-- Правка 22.08.2026 (Наталья): FBY-магазин у нас пустой и с выключенным API; реальные остатки
-- лежат по FBS-кампаниям (по одному нашему складу на кампанию). Таблица 507 была пуста —
-- переименовываем и добавляем тип размещения, чтобы отличать FBY от FBS.
ALTER TABLE IF EXISTS ya_fby_stock RENAME TO ya_mp_stock;
ALTER TABLE ya_mp_stock ADD COLUMN IF NOT EXISTS placement text;   -- FBY | FBS
ALTER TABLE ya_mp_stock ADD COLUMN IF NOT EXISTS campaign_name text;
ALTER INDEX IF EXISTS ya_fby_stock_day RENAME TO ya_mp_stock_day;

-- migrations/004_products_cost_seb.sql — реальная себестоимость товара.
-- cost_seb = расчётная себестоимость из report/stock (из приёмок), НЕ buyPrice.
-- buyPrice оказался кривой справочной ценой (завышен 20-50%) — для COGS не использовать.
ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_seb NUMERIC;

-- The current supplier-stock collector writes this optional turnover field.
ALTER TABLE supplier_stock ADD COLUMN IF NOT EXISTS sold_30d NUMERIC;

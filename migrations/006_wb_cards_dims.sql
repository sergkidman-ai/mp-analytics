-- migrations/006_wb_cards_dims.sql — габариты карточек WB (см, кг) + объём в литрах.
-- WB считает логистику по объёму в литрах: volume_l = Д×Ш×В / 1000. Плотность кг/л
-- выявляет подозрительные карточки (большой вес при малых габаритах и наоборот).
ALTER TABLE wb_cards ADD COLUMN IF NOT EXISTS length_cm  NUMERIC;
ALTER TABLE wb_cards ADD COLUMN IF NOT EXISTS width_cm   NUMERIC;
ALTER TABLE wb_cards ADD COLUMN IF NOT EXISTS height_cm  NUMERIC;
ALTER TABLE wb_cards ADD COLUMN IF NOT EXISTS weight_kg  NUMERIC;
ALTER TABLE wb_cards ADD COLUMN IF NOT EXISTS volume_l   NUMERIC;
ALTER TABLE wb_cards ADD COLUMN IF NOT EXISTS dims_valid BOOLEAN;

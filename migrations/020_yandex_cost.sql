-- 020: себестоимость Маркета по offerId — из МС-заказов «Покупатель Маркет» (как Озон/ВБ).
-- cost_per_unit = Σ(cost_seb позиций МС) / Σ(qty) по external_code (=offerId). 100% покрытие
-- проданных позиций, т.к. берём реальные ms_id из заказов, а не карточку (там нулевые дубли).
CREATE TABLE IF NOT EXISTS yandex_cost (
    offer TEXT PRIMARY KEY,
    cost_per_unit NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT now()
);

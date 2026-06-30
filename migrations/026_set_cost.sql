-- Себестоимость наборов = Σ закупочных компонентов. Состав — из thecartridge.ru (mix_data),
-- цена компонентов — из МойСклад (ms_product.buy_price). Состав кешируется (резолв один раз),
-- цена пересчитывается ежедневно. Ключ — external_code набора.
CREATE TABLE IF NOT EXISTS set_cost (
    external_code TEXT PRIMARY KEY,
    components    TEXT[],          -- external_code компонентов
    n_components  INT,
    cost          NUMERIC,         -- Σ min(buy_price) компонентов
    covered       INT,             -- сколько компонентов с ценой в МС
    resolved_at   TIMESTAMPTZ,     -- когда состав получен из API
    updated_at    TIMESTAMPTZ DEFAULT now()
);

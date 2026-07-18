-- 044_ms_return_cogs.sql — возвраты покупателей МойСклад (entity/salesreturn) для сторно COGS.
-- Когда покупатель возвращает товар, МС создаёт salesreturn со ссылкой demand на исходную
-- отгрузку; demand.name = WB assembly_id → мост к себесту (ms_demand_cogs) и к nm (raw_wb_report).
-- Склад назначения делит судьбу: всё, КРОМЕ «Брак», — продаваемый сток (COGS сторнируем при
-- перепродаже, иначе задвоение); «Брак» — дефект (себест остаётся расходом). Витрина margin_by_sku
-- читает sellable-гейт по demand_name и вычитает сторно в месяце возврата. Идемпотентно по return_id.
CREATE TABLE IF NOT EXISTS ms_return_cogs (
    return_id   TEXT PRIMARY KEY,          -- id документа salesreturn МС
    return_name TEXT,                       -- номер возврата (НЕ ключ к себесту — см. demand_name)
    org         TEXT,                        -- юрлицо (Цифровой/Дисквэр) — id организации МС
    agent       TEXT,                        -- контрагент (Покупатель ВБ)
    demand_name TEXT,                        -- имя исходной отгрузки = WB assembly_id (мост к nm/себесту)
    moment      TIMESTAMPTZ,                 -- дата/время возврата
    ym          TEXT,                        -- YYYY-MM (месяц возврата, для аналитики)
    store       TEXT,                        -- склад назначения (Звездный/Дисквер/Кантемировская/Брак)
    sellable    BOOLEAN NOT NULL,            -- TRUE = сток (сторнируем), FALSE = Брак (оставляем)
    ret_qty     NUMERIC,                     -- Σ positions.quantity возврата (аудит)
    unit_cogs   NUMERIC,                     -- себест/шт исходной отгрузки из кэша (cogs/qty), ₽
    storno_cogs NUMERIC,                     -- МС-оценка сторно = unit_cogs*ret_qty (аудит; витрина
                                             --   считает своё по строкам Возврат raw_wb_report)
    loaded_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ms_return_cogs_org_sellable ON ms_return_cogs(org, sellable);
CREATE INDEX IF NOT EXISTS idx_ms_return_cogs_demand ON ms_return_cogs(demand_name);

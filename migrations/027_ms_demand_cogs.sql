-- 027_ms_demand_cogs.sql — кэш себестоимости отгрузок МойСклад (report/stock/byoperation).
-- Себест КОНКРЕТНОЙ отгрузки = Σ positions.cost (FIFO на moment документа). Сырьё отдельно
-- от расчётов: тут — готовая цифра из МС по натуральному ключу demand_id; витрина матчит по
-- demand_name (= WB assembly_id). Идемпотентно: повторный сбор не плодит дублей.
CREATE TABLE IF NOT EXISTS ms_demand_cogs (
    demand_id   TEXT PRIMARY KEY,          -- id документа отгрузки МС
    demand_name TEXT NOT NULL,             -- имя отгрузки = WB assembly_id
    org         TEXT,                      -- юрлицо (Цифровой/Дисквэр) — id организации МС
    moment      TIMESTAMPTZ,               -- дата/время документа (на неё считается FIFO-себест)
    cogs        NUMERIC NOT NULL,          -- себест всей отгрузки, ₽ (Σ positions.cost/100)
    qty         NUMERIC,                   -- Σ positions.quantity (для импутации себест/шт)
    npos        INT,                       -- число позиций в отчёте
    loaded_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ms_demand_cogs_name ON ms_demand_cogs(demand_name);
CREATE INDEX IF NOT EXISTS idx_ms_demand_cogs_org_moment ON ms_demand_cogs(org, moment);

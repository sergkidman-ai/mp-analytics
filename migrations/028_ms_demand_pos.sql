-- 028_ms_demand_pos.sql — позиции отгрузок МС из report/stock/byoperation (детализация к 027).
-- Одна строка = товар в отгрузке: cost (₽, ИТОГ по строке с учётом qty, FIFO на moment документа).
-- Зачем: себест/шт по ms_id из РЕАЛЬНЫХ отгрузок — источник импутации FBO (WB-продажи со склада WB
-- без отгрузки продавца) вместо дырявого cost_seb. byoperation агрегирует дубль-позиции => PK валиден.
CREATE TABLE IF NOT EXISTS ms_demand_pos (
    demand_id TEXT NOT NULL REFERENCES ms_demand_cogs(demand_id) ON DELETE CASCADE,
    ms_id     TEXT NOT NULL,             -- товар МС
    cost      NUMERIC NOT NULL,          -- себест строки, ₽ (итог, НЕ за шт)
    qty       NUMERIC NOT NULL,          -- количество (бывает 0 — «в минус», cost всё равно есть)
    PRIMARY KEY (demand_id, ms_id)
);
CREATE INDEX IF NOT EXISTS idx_ms_demand_pos_ms ON ms_demand_pos(ms_id);

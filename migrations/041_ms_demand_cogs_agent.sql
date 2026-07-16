-- 041: агент отгрузки нужен для разделения кэша FIFO-себестоимости WB и Ozon.
ALTER TABLE ms_demand_cogs ADD COLUMN IF NOT EXISTS agent TEXT;

-- Все данные, собранные до этой миграции, относятся к WB.
UPDATE ms_demand_cogs SET agent='Покупатель ВБ' WHERE agent IS NULL;

CREATE INDEX IF NOT EXISTS idx_ms_demand_cogs_org_agent ON ms_demand_cogs(org, agent);

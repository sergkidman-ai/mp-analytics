-- 013: дата последней закупки по поставщику (из приёмок МойСклад entity/supply).
-- Для выявления «спящих» поставщиков на странице Поставщики (кандидаты на удаление).
CREATE TABLE IF NOT EXISTS supplier_last_purchase (
    supplier TEXT PRIMARY KEY,
    last_supply DATE,
    supply_count_90d INT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

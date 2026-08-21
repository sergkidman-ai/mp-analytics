-- поток: ev — позиции ПРИЁМОК МойСклада (цена закупки партии на дату документа).
-- Нужны, чтобы оценивать товар, лежавший на складе МП: FIFO отгрузок отвечает на вопрос
-- «по чём списали при продаже», а тут вопрос другой — «по чём мы купили то, что лежало».
CREATE TABLE IF NOT EXISTS ms_supply_pos (
    supply_id   text        NOT NULL,
    supply_name text,
    moment      timestamp   NOT NULL,
    ms_id       text        NOT NULL,
    qty         numeric,
    price_rub   numeric,
    agent       text,
    loaded_at   timestamp   DEFAULT now(),
    PRIMARY KEY (supply_id, ms_id)
);
CREATE INDEX IF NOT EXISTS ms_supply_pos_ms_moment ON ms_supply_pos (ms_id, moment DESC);

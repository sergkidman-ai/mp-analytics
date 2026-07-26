-- поток: rev
-- Ручной себест Маркета ПО ОТГРУЗКАМ — для заказов, где FIFO=0 И товара нет в справочнике
-- (импутация пуста). Сотрудник заполняет через форму в разделе «Себестоимость» (детальный отчёт).
-- Ключ — номер отгрузки (per-order), поэтому отдельно от per-article cogs_manual.
-- Приоритет в коллекторе ya_cogs_demand: manual > ms_fifo > imputed (ручная правка = истина).
CREATE TABLE IF NOT EXISTS ya_cogs_manual (
    account      TEXT NOT NULL,
    demand_name  TEXT NOT NULL,          -- = номер отгрузки/заказа Яндекс.Маркета
    cogs         NUMERIC NOT NULL,       -- ручной себест на всю отгрузку (₽)
    note         TEXT,
    author       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, demand_name)
);

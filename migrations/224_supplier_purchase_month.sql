-- поток: inv — сумма закупки по ГРУППЕ поставщика за месяц (страница «Поставщики»).
-- Источник: приёмки МойСклада (entity/supply, проведённые) и возвраты поставщику
-- (entity/purchasereturn) — фактически принятый/возвращённый товар по дате документа.
-- Группа = группа юрлиц из invoice_bot/supplier_groups.py: у поставщика за годы менялись
-- ООО/ИП, реальный объём закупки виден только по группе целиком.
-- Витрина статична: прошлые месяцы пересчитываются только явным --since, ночью — текущий.
CREATE TABLE IF NOT EXISTS supplier_purchase_month (
    month       date    NOT NULL,          -- первое число месяца
    grp         text    NOT NULL,          -- группа поставщика (или имя контрагента вне групп)
    supply_sum  numeric NOT NULL DEFAULT 0,  -- приёмки, ₽ с НДС
    supply_docs int     NOT NULL DEFAULT 0,
    return_sum  numeric NOT NULL DEFAULT 0,  -- возвраты поставщику, ₽ с НДС
    return_docs int     NOT NULL DEFAULT 0,
    in_group    boolean NOT NULL DEFAULT true,  -- false = контрагента нет в таблице групп
    updated_at  timestamptz DEFAULT now(),
    PRIMARY KEY (month, grp)
);
CREATE INDEX IF NOT EXISTS supplier_purchase_month_m ON supplier_purchase_month (month DESC);

-- Кэш имён контрагентов МС: чтобы не дёргать МС ради названия по каждому агенту вне групп.
CREATE TABLE IF NOT EXISTS ms_agent_name (
    agent_id   text PRIMARY KEY,
    name       text,
    updated_at timestamptz DEFAULT now()
);

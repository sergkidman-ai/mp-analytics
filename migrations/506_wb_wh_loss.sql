-- поток: ev — витрина утраты товара на пострадавших складах ВБ (для раздела «Склад» дашборда).
-- Строка = SKU на складе на дату удара. Себест — цена приёмки МС не позже даты удара.
CREATE TABLE IF NOT EXISTS wb_wh_loss (
    warehouse    text NOT NULL,          -- имя склада в отчёте ВБ
    wh_label     text,                   -- как склад называют в новостях
    status       text NOT NULL,          -- lost = уничтожен, damaged = серьёзно повреждён
    hit_date     date NOT NULL,
    snap_date    date NOT NULL,          -- дата снимка остатков, по которому считали
    snap_how     text,                   -- «день удара» / «ближайший до» / «ближайший после»
    account      text NOT NULL,
    nm_id        bigint NOT NULL,
    vendor_code  text,
    qty          integer NOT NULL,
    components   integer,                -- картриджей в карточке (набор CMYK = 4)
    unit_cost    numeric,                -- себест единицы ВБ по приёмке
    supply_date  date,                   -- дата той приёмки
    cost_total   numeric,
    vitrina_u    numeric,                -- сверочный метод: mkt_sku_economics
    vitrina_src  text,
    built_at     timestamp DEFAULT now(),
    PRIMARY KEY (warehouse, account, nm_id)
);
CREATE INDEX IF NOT EXISTS wb_wh_loss_status ON wb_wh_loss (status, hit_date);

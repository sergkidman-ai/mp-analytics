-- поток: mkt
-- «Вышла из сумрака»: журнал SKU, по которым у TheCartridge ВПЕРВЫЕ появилась живая закупка.
-- Пока цены нет — позиции обычно нет и в наличии: считать по ней нечего, себест не выдумываем
-- (см. reports/sku_economics.py, п. 3d). Как только цена появилась — экономика считается
-- обычной формулой, а ops/tc_price_alert.py шлёт алерт РОВНО ОДИН РАЗ на карточку.
CREATE TABLE IF NOT EXISTS mkt_tc_resurfaced (
    account        text        NOT NULL,
    nm_id          bigint      NOT NULL,
    external_code  text        NOT NULL,
    first_price_on date        NOT NULL,          -- дата снимка, в котором появилась цена
    buy_price      numeric,                       -- закупка на момент появления
    margin_own     numeric,                       -- маржа от нашей цены в тот же день, % (может быть NULL)
    notified_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, nm_id)
);

COMMENT ON TABLE mkt_tc_resurfaced IS
  'SKU, по которым живая закупка TheCartridge появилась впервые. Один ряд = один алерт.';

-- 108_mkt_margin_control.sql — mkt-блок. Ежедневный контроль маржи на ЖИВОЙ (восстановительной)
-- себестоимости TheCartridge (tc_buy_price). Снимок на день по SKU.
-- ВАЖНО: маржа-контроль считается на buy_price_live («почём купим сегодня»), РЯДОМ показываем
-- FIFO-себест из отгрузок МС (fin, read-only mkt_sku_economics.cogs_u) и их расхождение — НЕ замена.
-- Цена/комиссия/логистика — из витрины mkt_sku_economics (форвард payout-ratio). Пишем только эту таблицу.

CREATE TABLE IF NOT EXISTS mkt_margin_control (
    captured_date  date        NOT NULL,
    account        text        NOT NULL,
    nm_id          bigint      NOT NULL,
    vendor_code    text,
    external_code  text,                 -- код платформы (маппинг nm→МС externalCode)
    map_source     text,                 -- как смаплено: shipment|barcode|vendor|prefix|NULL
    subject        text,
    -- цена/экономика (форвард, из mkt_sku_economics)
    our_price      numeric,              -- наша промо-цена (до СПП) = база KPI-маржи
    buyer_price    numeric,              -- цена покупателя (после СПП)
    payout_ratio   numeric,
    to_pay_u       numeric,              -- к перечислению/шт = our_price*payout
    logistics_u    numeric,
    storage_u      numeric,
    accept_u       numeric,
    -- ДВЕ себестоимости рядом
    buy_price_live numeric,              -- живая закупочная (TheCartridge); NULL если no_price/unmapped
    buy_status     text        NOT NULL, -- 'ok'(цена сегодня) | 'stale'(послед. известная) | 'no_price' | 'unmapped'
    price_date     date,                 -- дата цены (сегодня для ok; прошлая для stale)
    fifo_cogs_u    numeric,              -- FIFO из отгрузок МС (mkt_sku_economics.cogs_u), справочно
    cogs_delta     numeric,              -- buy_price_live − fifo_cogs_u (>0: перезакупка дороже факта)
    -- маржа на живой себестоимости (KPI: от нашей цены)
    net_live       numeric,              -- to_pay − logistics − storage − accept − buy_price_live
    margin_own_live numeric,             -- 100*net_live/our_price
    -- маржа на FIFO (справочно, из витрины)
    net_fifo       numeric,
    margin_own_fifo numeric,
    -- флаги контроля
    below_threshold boolean     NOT NULL DEFAULT false,  -- margin_own_live < порога
    is_negative     boolean     NOT NULL DEFAULT false,  -- net_live < 0
    threshold_pct   numeric,                              -- порог, действовавший в прогоне
    built_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (captured_date, account, nm_id)
);

CREATE INDEX IF NOT EXISTS idx_mmc_flags ON mkt_margin_control (captured_date, below_threshold, is_negative);
CREATE INDEX IF NOT EXISTS idx_mmc_status ON mkt_margin_control (captured_date, buy_status);
CREATE INDEX IF NOT EXISTS idx_mmc_nm ON mkt_margin_control (account, nm_id, captured_date DESC);

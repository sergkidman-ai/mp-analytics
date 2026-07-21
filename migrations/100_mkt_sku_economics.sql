-- 100_mkt_sku_economics.sql — mkt-блок (Маркетинг). Витрина юнит-экономики ПО ВСЕМ SKU acc1,
-- включая непроданные (импутация медианными ставками расходов). READ-ONLY по margin_by_sku/ms_product.
-- Маржа считается ОТ ЦЕНЫ РЕАЛИЗАЦИИ ВБ (revenue_wb, после СПП).

-- Текущие карточные цены WB (Prices API, отдельный токен WB_TOKEN_PRICES_*).
CREATE TABLE IF NOT EXISTS wb_price (
    account          text        NOT NULL,
    nm_id            bigint      NOT NULL,
    vendor_code      text,
    price            numeric,          -- цена до скидки продавца (list)
    discounted_price numeric,          -- наша цена после скидки продавца (ДО СПП) = карточная
    discount_pct     numeric,
    club_price       numeric,
    currency         text,
    captured_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, nm_id)
);

-- Витрина юнит-экономики по SKU (форвардная, при текущей цене + факт для проданных).
CREATE TABLE IF NOT EXISTS mkt_sku_economics (
    account            text        NOT NULL,
    nm_id              bigint      NOT NULL,
    vendor_code        text,
    subject            text,
    -- цена и себест
    price_card         numeric,          -- discounted_price (наша цена до СПП, текущая)
    cogs_u             numeric,          -- замещающая себест/шт
    cogs_source        text,             -- 'shipment' | 'barcode' | NULL
    -- расходные ставки (факт для проданных, медиана для непроданных)
    spp_pct            numeric,
    commission_pct     numeric,
    -- форвардная экономика/шт при текущей цене
    revenue_wb_u       numeric,          -- price_card * (1 - spp) = реализация ВБ/шт
    commission_u       numeric,
    logistics_u        numeric,
    storage_u          numeric,
    accept_u           numeric,
    net_u              numeric,          -- revenue_wb_u - commission - logistics - storage - accept - cogs
    margin_pct_wb      numeric,          -- net_u / revenue_wb_u  (ГЛАВНОЕ: маржа от цены реализации ВБ)
    -- факт (для проданных, сверка)
    sold_flag          boolean     NOT NULL DEFAULT false,
    qty_period         numeric,
    net_u_actual       numeric,
    margin_pct_wb_actual numeric,
    period_econ        date,             -- месяц источника фактических ставок
    built_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, nm_id)
);

CREATE INDEX IF NOT EXISTS idx_mkt_sku_econ_margin ON mkt_sku_economics (account, margin_pct_wb);
CREATE INDEX IF NOT EXISTS idx_mkt_sku_econ_sold   ON mkt_sku_economics (account, sold_flag);

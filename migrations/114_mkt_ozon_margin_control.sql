-- поток: mkt
-- 114_mkt_ozon_margin_control.sql — контроль маржи Ozon (аналог 108 для ВБ).
--
-- KPI: чистая ≥ порога (по умолчанию 25 %) от НАШЕЙ цены — той, что видит Ozon как базу
-- комиссии, а не от цены покупателя (она ниже на субсидию Озона, см. mkt_ozon_buyer_price).
--
-- Модель на штуку:
--   to_pay_u = our_price × payout_ratio          (payout из financial_data = цена − комиссия)
--   net_u    = to_pay_u − logistics_u − storage_u − accept_u − returns_u − other_u − cogs_u
--   margin_own = 100 × net_u / our_price
-- Расходы площадки на штуку — из витрины fin `margin_by_sku` (read-only) за последний полный
-- месяц, делённые на qty из постингов: в margin_by_sku у Ozon `qty` = NULL by design.
-- ВНИМАНИЕ: у Ozon `other` в margin_by_sku УЖЕ содержит рекламу, баллы, подписку, эквайринг
-- и штрафы (см. reports/margin_ozon_sku.py) — отдельно их НЕ добавлять, будет двойной счёт.
--
-- Две себестоимости рядом, как на ВБ: живая (tc_buy_price, «почём купим сегодня») — для решений,
-- FIFO из margin_by_sku.cogs — справочно, плюс их расхождение.
--
-- Предел снижения цены (главный ответ шага 2): цена, при которой маржа падает ДО порога, —
--   price_at_threshold = (постоянные расходы на штуку + себест) / (payout_ratio − порог),
-- допущение: постоянные расходы на штуку не зависят от цены (реклама внутри `other` на деле
-- частично пропорциональна цене → оценка консервативна).
-- Пересечение с индексом цен: price_for_target из mkt_ozon_buyer_price — цена для выхода
-- в зелёную зону. verdict говорит, укладывается ли выход в зелёную зону в KPI по марже.

CREATE TABLE IF NOT EXISTS mkt_ozon_margin_control (
    captured_date      date        NOT NULL,
    account            text        NOT NULL,
    offer_id           text        NOT NULL,
    sku                bigint,
    name               text,
    -- цена
    our_price          numeric,                -- наша цена, база комиссии и KPI-маржи
    buyer_price        numeric,                -- цена покупателя (наша × k), справочно
    payout_ratio       numeric,                -- payout / цена, из фактических продаж
    payout_source      text,                   -- 'факт' | 'аккаунт' | 'комиссия' (из commission_fbo_pct)
    to_pay_u           numeric,
    -- расходы площадки на штуку (fin, read-only)
    logistics_u        numeric,
    storage_u          numeric,
    accept_u           numeric,
    returns_u          numeric,
    other_u            numeric,                -- реклама + баллы + подписка + эквайринг + штрафы
    cost_source        text,                   -- 'факт' (свои продажи) | 'аккаунт' (медиана)
    -- две себестоимости
    buy_price_live     numeric,
    buy_status         text        NOT NULL,   -- 'ok' | 'stale' | 'no_price' | 'unmapped'
    buy_map_source     text,                   -- 'offer' | 'prefix4'
    price_date         date,
    fifo_cogs_u        numeric,
    cogs_delta         numeric,                -- живая − FIFO
    cogs_u             numeric,                -- что реально легло в расчёт
    cogs_source        text,                   -- 'живая' | 'fifo'
    -- маржа
    net_live           numeric,
    margin_own_live    numeric,
    net_fifo           numeric,
    margin_own_fifo    numeric,
    -- контроль и предел снижения
    below_threshold    boolean     NOT NULL DEFAULT false,
    is_negative        boolean     NOT NULL DEFAULT false,
    threshold_pct      numeric,
    price_at_threshold numeric,                -- цена, при которой маржа = порогу
    discount_limit_pct numeric,                -- на сколько % можем упасть, не пробив KPI
    -- пересечение с индексом цен
    color_index        text,
    external_index     numeric,
    price_for_target   numeric,                -- цена для выхода в зелёную зону
    target_discount_pct numeric,               -- сколько % снижения требует зелёная зона
    verdict            text,                   -- 'уже_зелёный' | 'можно_снижать' | 'не_укладывается'
                                               -- | 'нет_индекса' | 'нет_себеста'
    built_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (captured_date, account, offer_id)
);

CREATE INDEX IF NOT EXISTS idx_mozmc_flags
    ON mkt_ozon_margin_control (captured_date, below_threshold, is_negative);
CREATE INDEX IF NOT EXISTS idx_mozmc_verdict
    ON mkt_ozon_margin_control (captured_date, account, verdict);
CREATE INDEX IF NOT EXISTS idx_mozmc_sku
    ON mkt_ozon_margin_control (account, sku, captured_date DESC);

-- поток: mkt
-- Цена покупателя Ozon по каждому SKU.
--
-- Зачем: индекс цен Ozon считается от цены, которую видит ПОКУПАТЕЛЬ (наша цена минус
-- скидка за счёт Озона), а не от нашей. Прямого источника этой цены в Seller API нет —
-- проверено 07.08.2026 на пяти эндпоинтах (см. docs/MKT_OZON_PLAN.md §1.4):
--   /v5/product/info/prices   — поля marketing_price в схеме больше нет;
--                               marketing_seller_price = 0.969 × price (наша же скидка);
--   /v3/product/info/list     — тот же блок price, цены покупателя нет;
--   отчёт по товарам          — «Текущая цена с учетом скидки» = наша цена (медиана 1.049
--                               от price), «Цена Premium» пуста у всех 22 389 строк;
--   /v1/actions/products      — action_price = наша цена в акции (~0.90), не цена витрины;
--   /v1/product/info/discounted — только уценённые, пусто.
-- Поэтому цена покупателя ВОССТАНАВЛИВАЕТСЯ: buyer = our_price × k, где k — доля,
-- измеренная по фактическим продажам (financial_data.customer_price / price).
--
-- Обоснование модели (замеры 07.08.2026, acc1, июль):
--   * субсидия Озона — ПРОЦЕНТ, не фиксированная сумма: корреляция суммы субсидии
--     с ценой +0.97, корреляция k с уровнем цены −0.09 (k ≈ 0.54…0.62 во всех корзинах);
--   * k устойчив во времени: |k(июль) − k(июнь)| медиана 0.032, у 91 % SKU < 0.10;
--   * снижение НАШЕЙ цены доходит до покупателя: на 59 SKU с изменением цены
--     июнь→июль передача Δ медиана 1.17 (p25 0.96), корреляция +0.76.
-- Следствие: чтобы двинуть индекс к цели, достаточно нашей цены — субсидия её не гасит.

create table if not exists mkt_ozon_buyer_price (
    account            text        not null,
    offer_id           text        not null,
    snapshot_date      date        not null,   -- дата снимка ozon_price_index
    sku                bigint,
    our_price          numeric,                -- наша цена, база комиссии и выплаты
    k                  numeric,                -- цена покупателя / наша цена
    k_source           text,                   -- 'факт' — по продажам этого SKU;
                                               -- 'аккаунт' — медиана аккаунта (нет продаж)
    k_sales            int,                    -- сколько продаж легло в расчёт k
    buyer_price        numeric,                -- our_price × k — цена, которую видит покупатель
    external_min_price numeric,                -- минимальная цена конкурента (внешний индекс)
    external_index     numeric,
    color_index        text,
    price_for_target   numeric,                -- наша цена, при которой индекс = target
    target_index       numeric,
    built_at           timestamptz not null default now(),
    primary key (account, offer_id, snapshot_date)
);

create index if not exists mkt_ozon_buyer_price_zone_idx
    on mkt_ozon_buyer_price (account, snapshot_date, color_index);
create index if not exists mkt_ozon_buyer_price_sku_idx
    on mkt_ozon_buyer_price (account, sku);

-- Журнал прогонов: чем закончился расчёт и какова его точность на контрольной выборке.
create table if not exists mkt_ozon_buyer_price_run (
    account        text        not null,
    snapshot_date  date        not null,
    rows_total     int,
    rows_k_fact    int,
    k_median       numeric,
    check_n        int,                        -- SKU в сверке с фактом
    check_median   numeric,                    -- медиана buyer_price / фактическая цена
    check_within10 numeric,                    -- доля сверки в пределах ±10 %
    built_at       timestamptz not null default now(),
    primary key (account, snapshot_date)
);

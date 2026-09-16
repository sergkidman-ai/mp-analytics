-- 123_wb_promo_guard.sql — журнал сторожа автоакций WB (ops/wb_promo_guard.py).
--
-- Зачем таблица, а не только CSV: сторож ходит волнами (T+15м, T+2ч, T+8ч), и на второй
-- волне ему нужно знать ДОАКЦИОННУЮ цену — ту, что была до того, как ВБ срезал её ночью.
-- Из wb_price её уже не взять: коллектор в 09:20 перезапишет снимок акционными ценами.
-- Поэтому «цену до» фиксируем здесь на первой волне и дальше берём отсюда.
--
-- Одна строка = одно решение по одному nm_id в одной волне (в т.ч. решение «не трогаем»
-- со status='skip' — нужно, чтобы разбирать постфактум, почему товар остался в минусе).

CREATE TABLE IF NOT EXISTS wb_promo_guard_log (
    ts             timestamptz NOT NULL DEFAULT now(),
    account        text        NOT NULL,
    nm_id          bigint      NOT NULL,
    vendor_code    text,
    external_code  text,
    wave           text,                  -- метка волны: 'start' | 'h2' | 'h8' | 'manual'
    promo_ids      text,                  -- id акций, активных на момент прогона (через запятую)

    price_before   numeric,               -- базовая цена карточки до нашей правки
    disc_before    numeric,               -- скидка карточки, % (то, что выставил ВБ)
    buyer_before   numeric,               -- цена после скидки, ДО СПП = база маржи
    prepromo_price numeric,               -- цена до акции (снимок wb_price или первая волна)

    cogs           numeric,
    cogs_source    text,                  -- 'own_stock' | 'supplier_min' | 'fifo' | 'tc' | NULL
    stock_branch   text,                  -- 'own' | 'remote' | 'none'
    retention      numeric,               -- доля удержаний МП, применённая в расчёте
    floor_net      numeric,               -- требуемая чистая: max(300; 10 % от себестоимости)
    floor_price    numeric,               -- наш пол по цене = (floor_net + cogs) / (1 - retention)
    net_before     numeric,               -- чистая при текущей цене (минус = продаём в убыток)

    target_price   numeric,               -- что отправили в ВБ
    status         text,                  -- 'skip' | 'sent' | 'confirmed' | 'error' | 'dry'
    reason         text,                  -- человекочитаемая причина решения
    err            text,                  -- текст ошибки ВБ, если была
    price_after    numeric                -- перечитанная цена после применения (подтверждение)
);

CREATE INDEX IF NOT EXISTS wb_promo_guard_log_ts_idx  ON wb_promo_guard_log (ts DESC);
CREATE INDEX IF NOT EXISTS wb_promo_guard_log_nm_idx  ON wb_promo_guard_log (account, nm_id, ts DESC);

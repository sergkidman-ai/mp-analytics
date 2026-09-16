-- 124_oz_promo_guard.sql — поток: mkt — журнал сторожа акций Ozon (ops/ozon_promo_guard.py).
--
-- Отдельно от oz_action_log: тот ведёт лестница белых акций, и rejected_recently читает его
-- отказы. Сторож смотрит ВСЕ остальные акции, его решения в тот журнал не смешиваем.
-- Одна строка = одно решение по одному товару в одной акции в одной волне, включая «проходит»
-- (status='ok') — чтобы постфактум видеть, почему товар остался.

create table if not exists oz_promo_guard_log (
    id            bigserial   primary key,
    ts            timestamptz not null default now(),
    account       text        not null,
    wave          text        not null,                 -- метка прогона: manual | утро | …
    action_id     bigint      not null,
    action_title  text,
    product_id    bigint      not null,
    offer_id      text,
    name          text,
    action_price  numeric(12,2),                        -- цена в акции до решения
    max_price     numeric(12,2),                        -- потолок акции (max_action_price)
    cogs          numeric(12,2),
    cogs_source   text,
    keep_ratio    numeric(6,4),
    floor_price   numeric(12,2),
    new_price     numeric(12,2),                        -- куда поднимаем (mode = raise)
    mode          text        not null,                 -- ok | raise | remove
    reason        text,
    status        text        not null,                 -- dry | sent | confirmed | error
    note          text                                  -- ответ площадки / сверка
);

create index if not exists oz_promo_guard_log_ts on oz_promo_guard_log (account, ts);
create index if not exists oz_promo_guard_log_pid on oz_promo_guard_log (account, product_id, ts);

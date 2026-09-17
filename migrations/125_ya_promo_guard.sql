-- 125_ya_promo_guard.sql — поток: mkt — журнал сторожа акций Маркета (ops/ya_promo_guard.py).
--
-- Отличие от Ozon: Маркет применяет update/delete через 4–6 часов, сверить сразу нельзя.
-- Поэтому строка живёт в статусе 'sent', пока СЛЕДУЮЩИЙ прогон не увидит результат и не
-- переведёт её в 'confirmed' или 'error'. Отсюда индекс по (status, ts).

create table if not exists ya_promo_guard_log (
    id              bigserial   primary key,
    ts              timestamptz not null default now(),
    account         text        not null,
    wave            text        not null,
    promo_id        text        not null,
    promo_name      text,
    mechanics       text,                           -- DIRECT_DISCOUNT | BLUE_FLASH | …
    offer_id        text        not null,           -- SKU продавца
    promo_status    text,                           -- участие: AUTO | MANUAL | RENEWED | …
    price           numeric(12,2),                  -- зачёркнутая цена
    promo_price     numeric(12,2),                  -- цена по акции до решения
    max_promo_price numeric(12,2),                  -- потолок акции
    cogs            numeric(12,2),
    cogs_source     text,
    keep_ratio      numeric(6,4),                   -- доля выручки после удержаний (плоская)
    floor_price     numeric(12,2),
    new_price       numeric(12,2),                  -- куда поднимаем (mode = raise)
    mode            text        not null,           -- ok | raise | remove | ok_deferred
    reason          text,
    status          text        not null,           -- dry | skip | sent | confirmed | error
    note            text
);

create index if not exists ya_promo_guard_log_pending on ya_promo_guard_log (status, ts);
create index if not exists ya_promo_guard_log_offer on ya_promo_guard_log (offer_id, ts);

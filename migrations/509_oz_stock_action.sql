-- поток: mkt — автоучастие в акциях Ozon «Распродажа стока»
-- Ступень лестницы живёт на ТОВАРЕ, а не на акции: акция идёт 14 дней, лестница — 4 недели,
-- поэтому ступень переносится в следующую акцию того же типа.

create table if not exists oz_action_ladder (
    account        text        not null,
    offer_id       text        not null,
    rung           smallint    not null default 0,     -- 0..4, шагов вниз от потолка
    floor_price    numeric(12,2),                      -- (себест + цель) / доля, что остаётся нам
    cap_price      numeric(12,2),                      -- потолок Ozon на момент последнего шага
    last_price     numeric(12,2),                      -- цена, с которой товар заведён сейчас
    cogs           numeric(12,2),
    cogs_source    text,                               -- откуда взят себест (для разбора)
    last_action_id bigint,
    last_step_on   date,                               -- когда двигали ступень (шаг раз в неделю)
    updated_at     timestamptz not null default now(),
    primary key (account, offer_id)
);

-- журнал заведений: что, куда, по какой цене и чем ответил Ozon
create table if not exists oz_action_log (
    id           bigserial primary key,
    ts           timestamptz not null default now(),
    account      text        not null,
    action_id    bigint      not null,
    action_title text,
    offer_id     text        not null,
    product_id   bigint,
    action_price numeric(12,2),
    stock        integer,
    rung         smallint,
    ok           boolean,
    note         text
);
create index if not exists oz_action_log_ts_idx on oz_action_log (ts desc);

-- акции, которые мы уже видели: для «новая акция» и напоминания за сутки до старта
create table if not exists oz_action_seen (
    account         text        not null,
    action_id       bigint      not null,
    title           text,
    date_start      timestamptz,
    date_end        timestamptz,
    freeze_date     timestamptz,
    whitelisted     boolean     not null default false,
    notified_new    boolean     not null default false,
    notified_start  boolean     not null default false,
    first_seen      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    primary key (account, action_id)
);

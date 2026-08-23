-- поток: card — здоровье карточек товара на площадках
--
-- Зачем: карточки падают в «Ошибки»/«На доработку» после апдейтов внешней системы ТК,
-- причём нестабильно. Площадка причину в API не отдаёт (уходит в «Историю обновлений»
-- по task_id, который остался у ТК). Своя таблица нужна, чтобы (1) видеть, СКОЛЬКО карточка
-- висит в ошибке — площадка этого не показывает, (2) не долбить одну и ту же карточку
-- бесконечно, (3) отличать «вылечили мы» от «починил ТК».
--
-- Существующий ozon_product статусов не хранит — только имя и флаг архива, поэтому узел новый.

-- Текущее состояние проблемной карточки. Живёт по натуральному ключу площадка+аккаунт+offer_id.
-- Строка НЕ удаляется после лечения: закрывается флагом is_open и датой healed_at,
-- чтобы видеть рецидивы (карточка, которую ТК роняет раз за разом, — сигнал программисту).
create table if not exists card_status (
    platform        text        not null,              -- ozon | wb | yandex (пока только ozon)
    account         text        not null,              -- oz_acc1 | oz_acc2
    offer_id        text        not null,              -- наш артикул = ключ связи с МС
    product_id      bigint,
    name            text,

    err_class       text,                              -- A | C | O (см. ниже)
    status          text,                              -- statuses.status
    status_failed   text,                              -- statuses.status_failed (пусто = здорова)
    moderate_status text,
    validation_status text,
    status_name     text,                              -- «Продается» / «Не создан» / …
    status_descr    text,
    err_codes       text,                              -- коды ERROR-уровня через пробел
    err_texts       text,                              -- тексты ERROR-уровня (для отчёта в ТК)

    is_selling      boolean     not null default false, -- торгует ли сейчас (риск для выручки)
    is_open         boolean     not null default true,  -- проблема ещё жива
    attempts        smallint    not null default 0,     -- сколько раз дожимали (потолок в коде)
    rung            smallint    not null default 1,     -- ступень лестницы повторов
    needs_human     boolean     not null default false, -- лестница исчерпана, нужен человек

    card_updated_at timestamptz,                       -- statuses.status_updated_at площадки
    first_seen      timestamptz not null default now(), -- когда УВИДЕЛИ в ошибке впервые
    last_seen       timestamptz not null default now(),
    last_attempt_at timestamptz,
    healed_at       timestamptz,

    primary key (platform, account, offer_id)
);

create index if not exists card_status_open_idx on card_status (platform, err_class, is_open);

-- Класс ошибки (проставляет детектор):
--   A — «Не обновлён»: status_failed=imported, ERROR-ошибок нет, модерация approved.
--       Причины в API не существует. Лечится ПОВТОРНОЙ ОТПРАВКОЙ ТОГО ЖЕ значения.
--   C — контентный ERROR модерации. Повтор бесполезен, правится только в ТК.
--   O — прочее (например, только warning-и): автомат не трогает, копим статистику.
comment on column card_status.err_class is 'A=повторимая, C=контент (в ТК), O=прочее';

-- Журнал попыток дожима: одна строка на одну отправку. Возобновляемость прогона и разбор
-- постфактум держатся на нём. HTTP 200 успехом НЕ считается — успех это task_status=imported
-- плюс очистившийся status_failed при перечитке (bought отдельным полем healed).
create table if not exists card_push_log (
    id              bigserial   primary key,
    ts              timestamptz not null default now(),
    platform        text        not null,
    account         text        not null,
    offer_id        text        not null,
    product_id      bigint,
    rung            smallint    not null,              -- 1 = no-op по атрибуту
    attr_id         integer,                           -- какой атрибут отправлен обратно
    http_code       integer,
    task_id         bigint,                            -- ВОЗВРАЩАЕТСЯ В КОРНЕ ОТВЕТА Ozon
    task_status     text,                              -- imported | skipped | failed | pending
    healed          boolean,                           -- очистился ли status_failed при перечитке
    note            text
);

create index if not exists card_push_log_card_idx on card_push_log (platform, account, offer_id, ts desc);

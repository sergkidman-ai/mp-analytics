-- поток: ret — возвраты FBS (что физически надо забрать с ПВЗ)
-- Серия 3xx закреплена за потоком `ret` (0xx — fin, 1xx — mkt, 2xx — inv).

-- Сырьё как есть: любой пересчёт делается отсюда, без повторного похода в API.
CREATE TABLE IF NOT EXISTS raw_mp_returns (
    platform   text        NOT NULL,          -- ozon | yandex | wb
    account    text        NOT NULL,          -- oz_acc1 | oz_acc2 | ya_acc1 | wb_acc1 | wb_acc2
    return_id  text        NOT NULL,          -- id возврата на площадке
    payload    jsonb       NOT NULL,
    loaded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, account, return_id)
);

-- Нормализованная шапка возврата.
CREATE TABLE IF NOT EXISTS mp_returns (
    platform        text NOT NULL,
    account         text NOT NULL,
    return_id       text NOT NULL,
    campaign        text,              -- магазин/кампания (у Яндекса их три), у Ozon NULL
    order_number    text,
    return_type     text,              -- ClientReturn/Cancellation/FullReturn | RETURN/UNREDEEMED
    scheme          text,              -- Fbs | Fbo
    status_raw      text,              -- машинный статус площадки (sys_name / shipmentStatus)
    status_name     text,              -- человеческая подпись
    stage           text,              -- pickup | transit | attention | closed  (см. returns_bot/pending.py)
    pvz_id          text,              -- точка, КУДА ехать забирать (Ozon target_place)
    pvz_name        text,
    pvz_address     text,
    pvz_instruction text,
    where_now       text,              -- где возврат физически сейчас (Ozon place) — для «в пути»
    barcode         text,              -- штрихкод возврата, если площадка его отдаёт
    created_at      timestamptz,       -- когда возврат заведён на площадке
    arrived_at      timestamptz,       -- когда приехал в точку выдачи/на склад
    deadline_at     timestamptz,       -- до какой даты забрать (утилизация/pickupTillDate)
    storage_days    int,
    storage_sum     numeric(12, 2),    -- начислено за хранение
    amount          numeric(12, 2),    -- сумма возврата
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    gone_at         timestamptz,       -- пропал из выдачи API = забрали/закрыли
    PRIMARY KEY (platform, account, return_id)
);

CREATE INDEX IF NOT EXISTS idx_mp_returns_stage
    ON mp_returns (stage, platform, account) WHERE gone_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mp_returns_pvz
    ON mp_returns (pvz_address) WHERE gone_at IS NULL;

-- Состав возврата: у Ozon одна позиция, у Яндекса items[] может быть несколько.
CREATE TABLE IF NOT EXISTS mp_return_items (
    platform  text NOT NULL,
    account   text NOT NULL,
    return_id text NOT NULL,
    seq       int  NOT NULL,
    sku       text,
    offer_id  text,             -- наш артикул (ключ связи с МойСклад)
    name      text,
    qty       int,
    price     numeric(12, 2),
    PRIMARY KEY (platform, account, return_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_mp_return_items_offer ON mp_return_items (offer_id);

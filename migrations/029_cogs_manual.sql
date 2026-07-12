-- 029_cogs_manual.sql — ручные себестоимости/шт (последний фолбэк FBO-импутации).
-- Для товаров без какого-либо источника (нет FBS-истории, нет группы, нет состава, нет закупочной)
-- цифру диктует клиент, либо она добыта разово (напр. отчёт оборотов по старой карточке).
-- Наивысший приоритет ручного значения над автоматикой НЕ нужен: цепочка пробует авто-источники,
-- cogs_manual — замыкающий шаг. Ключ = площадка+артикул площадки (WB: nm_id).
CREATE TABLE IF NOT EXISTS cogs_manual (
    platform   TEXT NOT NULL DEFAULT 'wb',
    article    TEXT NOT NULL,              -- WB: nm_id
    unit_cost  NUMERIC NOT NULL,           -- себест за штуку, ₽
    source     TEXT,                       -- откуда цифра (клиент/факт старой отгрузки/…)
    note       TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (platform, article)
);

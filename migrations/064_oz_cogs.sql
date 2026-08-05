-- поток: fin
-- Раздел «Себестоимость» → Ozon (два юрлица: oz_acc1 «Цифровой квадрат», oz_acc2 «Дисквэр»).
-- Зеркало ya_cogs_* (миграции 054-056), но источник FIFO-себеста — уже собранный кэш ms_demand_cogs
-- (агенты «Покупатель Озон» / «Озон Экспресс», юрлицо различается полем org), поэтому коллектор
-- oz_cogs_demand в МойСклад ходит только за суммой документа отгрузки (наша цена).
-- Статус отгрузки: raw_ozon_posting.status (мост posting_number = demand.name) + МС salesreturn
-- через ms_return_cogs (склад возврата: наш сток / Брак / «Озон» = склад FBO площадки).
CREATE TABLE IF NOT EXISTS oz_cogs_demand (
    account      TEXT NOT NULL,          -- oz_acc1 (Цифровой квадрат) | oz_acc2 (Дисквэр)
    demand_name  TEXT NOT NULL,          -- = номер отправления Ozon (мост к постингу/возврату)
    demand_id    TEXT,                   -- id документа отгрузки МС (для report/stock/byoperation)
    ym           DATE NOT NULL,          -- месяц отгрузки (первое число, по demand.moment)
    demand_date  DATE,                   -- дата отгрузки
    our_sum      NUMERIC,                -- наша цена (sum МС-документа отгрузки)
    qty          NUMERIC,                -- штук в отгрузке
    cogs         NUMERIC,                -- себестоимость отгрузки
    method       TEXT,                   -- 'ms_fifo' | 'imputed' (фолбэк cost_seb) | 'manual'
    status       TEXT,                   -- done | return_stock | return_defect | return_ozon
                                         -- | unredeemed | other
    status_raw   TEXT,                   -- статус отправления Ozon как есть (delivered/cancelled/…)
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, demand_name)
);
CREATE INDEX IF NOT EXISTS oz_cogs_demand_ym ON oz_cogs_demand (account, ym);

-- Ручной себест ПО ОТГРУЗКАМ — там, где FIFO=0 И импутация пуста (товара нет в справочнике).
-- Приоритет в коллекторе: manual > ms_fifo > imputed (ручная правка = истина).
CREATE TABLE IF NOT EXISTS oz_cogs_manual (
    account      TEXT NOT NULL,
    demand_name  TEXT NOT NULL,          -- = номер отправления Ozon
    cogs         NUMERIC NOT NULL,       -- ручной себест на всю отгрузку (₽)
    note         TEXT,
    author       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, demand_name)
);

-- Закрытые (замороженные) месяцы себеста Ozon: человек проверил → себест финальный, коллектор месяц
-- не пересобирает. Разморозка = удаление строки. Per-month, отдельно по каждому юрлицу.
CREATE TABLE IF NOT EXISTS oz_cogs_frozen (
    account   TEXT NOT NULL,
    ym        DATE NOT NULL,             -- 1-е число закрытого месяца
    closed_by TEXT,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, ym)
);

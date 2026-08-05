-- поток: fin
-- Раздел «Себестоимость» → WB (два юрлица: wb_acc1 «Цифровой квадрат», wb_acc2 «Дисквэр»).
-- Зеркало oz_cogs_* (миграция 064). Источник FIFO-себеста — кэш ms_demand_cogs (агент
-- «Покупатель ВБ», юрлицо в поле org), поэтому коллектор wb_cogs_demand ходит в МойСклад только
-- за суммой документа отгрузки (наша цена).
-- Мост: demand.name = assembly_id (номер сборочного задания WB). Статус собирается из финотчёта
-- raw_wb_report (supplier_oper_name: «Продажа» / «Возврат») + МС salesreturn через ms_return_cogs
-- (склад возврата: наш сток / «Брак»). Аналога озоновского склада «Озон» у ВБ нет.
-- ВАЖНО: раздел покрывает FBS-отгрузки. FBO-продажи ВБ документа «Отгрузка» в МС не создают
-- (~11 % оборота) — их здесь нет, они считаются импутацией во вкладке «Отчёты МП · WB».
CREATE TABLE IF NOT EXISTS wb_cogs_demand (
    account      TEXT NOT NULL,          -- wb_acc1 (Цифровой квадрат) | wb_acc2 (Дисквэр)
    demand_name  TEXT NOT NULL,          -- = assembly_id (номер сборочного задания WB)
    demand_id    TEXT,                   -- id документа отгрузки МС (для report/stock/byoperation)
    ym           DATE NOT NULL,          -- месяц отгрузки (первое число, по demand.moment)
    demand_date  DATE,                   -- дата отгрузки
    our_sum      NUMERIC,                -- наша цена (sum МС-документа отгрузки)
    qty          NUMERIC,                -- штук в отгрузке
    cogs         NUMERIC,                -- себестоимость отгрузки
    method       TEXT,                   -- 'ms_fifo' | 'imputed' (фолбэк cost_seb) | 'manual'
    status       TEXT,                   -- done | return_stock | return_defect | return_wb
                                         -- | unreported | other
    status_raw   TEXT,                   -- что нашлось в финотчёте: 'Продажа' / 'Возврат' / NULL
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, demand_name)
);
CREATE INDEX IF NOT EXISTS wb_cogs_demand_ym ON wb_cogs_demand (account, ym);

-- Ручной себест ПО ОТГРУЗКАМ — там, где FIFO=0 И импутация пуста (товара нет в справочнике).
-- Приоритет в коллекторе: manual > ms_fifo > imputed (ручная правка = истина).
CREATE TABLE IF NOT EXISTS wb_cogs_manual (
    account      TEXT NOT NULL,
    demand_name  TEXT NOT NULL,          -- = assembly_id
    cogs         NUMERIC NOT NULL,       -- ручной себест на всю отгрузку (₽)
    note         TEXT,
    author       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, demand_name)
);

-- Закрытые (замороженные) месяцы себеста WB: человек проверил → себест финальный, коллектор месяц
-- не пересобирает. Разморозка = удаление строки. Per-month, отдельно по каждому юрлицу.
CREATE TABLE IF NOT EXISTS wb_cogs_frozen (
    account   TEXT NOT NULL,
    ym        DATE NOT NULL,             -- 1-е число закрытого месяца
    closed_by TEXT,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, ym)
);

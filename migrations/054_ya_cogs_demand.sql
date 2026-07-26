-- поток: rev
-- Кэш себестоимости Маркета ПО ОТГРУЗКАМ (demand) для раздела «Себестоимость».
-- Себест каждой отгрузки — FIFO report/stock/byoperation (как WB/Ozon), наша цена — demand.sum из МС,
-- статус — из raw_yandex_stats_order + МС salesreturn. Заполняется collectors/ya_cogs_demand.py.
CREATE TABLE IF NOT EXISTS ya_cogs_demand (
    account      TEXT NOT NULL,
    demand_name  TEXT NOT NULL,          -- = номер заказа Яндекс.Маркета (мост к заказу/возврату)
    demand_id    TEXT,                   -- id документа отгрузки МС (для report/stock/byoperation)
    ym           DATE NOT NULL,          -- месяц отгрузки (первое число, по demand.moment)
    demand_date  DATE,                   -- дата отгрузки
    our_sum      NUMERIC,                -- наша цена (sum МС-документа отгрузки)
    qty          NUMERIC,                -- штук в отгрузке
    cogs         NUMERIC,                -- себестоимость отгрузки
    method       TEXT,                   -- 'ms_fifo' (FIFO из МС) | 'imputed' (фолбэк cost_seb)
    status       TEXT,                   -- done | return_stock | return_defect | unredeemed | other
    status_raw   TEXT,                   -- статус заказа Яндекса как есть (DELIVERED/RETURNED/...)
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, demand_name)
);
CREATE INDEX IF NOT EXISTS ya_cogs_demand_ym ON ya_cogs_demand (account, ym);

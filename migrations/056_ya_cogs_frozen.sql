-- поток: rev
-- Закрытые (замороженные) месяцы себеста Маркета. Человек проверил месяц и закрыл → себест финальный,
-- коллектор ya_cogs_demand его больше не пересобирает (не ходит в МС за этими отгрузками).
-- Разморозка = удаление строки → следующий прогон соберёт месяц заново. Per-month (по строке на месяц).
CREATE TABLE IF NOT EXISTS ya_cogs_frozen (
    account   TEXT NOT NULL,
    ym        DATE NOT NULL,             -- 1-е число закрытого месяца
    closed_by TEXT,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, ym)
);

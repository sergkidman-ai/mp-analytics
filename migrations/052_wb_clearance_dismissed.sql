-- поток: rev
-- Закрытые позиции распродажи ВБ: остаток на складе ВБ стал 0, цену подняли — больше не следим.
-- Отдельная таблица (а не флаг в wb_clearance): лоадер на каждой перезагрузке делает DELETE+INSERT из
-- файла, поэтому «закрытость» держим здесь, чтобы она переживала перезалив файла распродажи.
CREATE TABLE IF NOT EXISTS wb_clearance_dismissed (
    account      TEXT        NOT NULL,
    nm_id        BIGINT      NOT NULL,
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, nm_id)
);

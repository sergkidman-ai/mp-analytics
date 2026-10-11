-- поток: fin
-- Коды списаний, уже увиденные еженедельным сторожем финансовых услуг.
CREATE TABLE IF NOT EXISTS fin_charge_watch (
    platform text NOT NULL,
    kind text NOT NULL,
    code text NOT NULL,
    first_seen date,
    last_seen date,
    notified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, kind, code)
);

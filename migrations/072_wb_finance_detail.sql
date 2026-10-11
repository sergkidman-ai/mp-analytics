-- поток: fin
-- Новый Finance API ВБ; старый reportDetailByPeriod отключён и возвращает 404.
CREATE TABLE IF NOT EXISTS raw_wb_finance_detail (
    account text NOT NULL,
    rrd_id bigint NOT NULL,
    report_date date NOT NULL,
    payload jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, rrd_id)
);
CREATE INDEX IF NOT EXISTS raw_wb_finance_detail_date_idx
    ON raw_wb_finance_detail (account, report_date);

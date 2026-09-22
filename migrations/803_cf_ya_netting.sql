-- поток: cf
-- 803_cf_ya_netting.sql — Яндекс Маркет: отчёт по платежам (united-netting), свёрнутый по неделе
-- доставки. Метода «Предстоящие выплаты» в API нет (проверено 22.09.2026); ближайшая замена —
-- строки со статусом «Будет переведён / будет удержан» = ещё не выплачено.
CREATE TABLE IF NOT EXISTS cf_ya_netting_week (
    account     text        NOT NULL,
    week_start  date        NOT NULL,              -- понедельник недели доставки
    pending     numeric(14,2) NOT NULL DEFAULT 0,  -- ждёт выплаты (переведено − удержано, ещё без платёжки)
    paid        numeric(14,2) NOT NULL DEFAULT 0,  -- уже в платёжках
    rows_cnt    int         NOT NULL DEFAULT 0,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, week_start)
);

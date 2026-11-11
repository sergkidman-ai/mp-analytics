-- поток: cf
-- Подтверждённый график ЛК имеет приоритет над расчётом выплаты по начислениям.
CREATE TABLE IF NOT EXISTS cf_ozon_payout_schedule (
    account       text          NOT NULL CHECK (account IN ('oz_acc1', 'oz_acc2')),
    payment_date  date          NOT NULL,
    amount        numeric(14,2) NOT NULL CHECK (amount >= 0),
    components    jsonb         NOT NULL DEFAULT '[]'::jsonb
                               CHECK (jsonb_typeof(components) = 'array'),
    source        text          NOT NULL,
    confirmed_at  timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (account, payment_date)
);

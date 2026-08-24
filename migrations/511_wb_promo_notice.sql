-- поток: ev — журнал уведомлений о старте акций WB (чтобы не слать одно и то же дважды)
CREATE TABLE IF NOT EXISTS wb_promo_notice (
    account     text        NOT NULL,
    promo_id    bigint      NOT NULL,
    promo_type  text,
    name        text,
    starts_at   timestamptz,
    ends_at     timestamptz,
    seen_at     timestamptz NOT NULL DEFAULT now(),
    notified_at timestamptz,
    PRIMARY KEY (account, promo_id)
);

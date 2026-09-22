-- поток: cf
-- 805_cf_mp_balance.sql — «Наши деньги на МП»: остаток лицевого счёта площадки (заработано,
-- но ещё не выплачено на расчётный счёт). Источник — официальные цифры площадок, НЕ расчёт
-- «начислено минус выплачено»: накопительный метод врёт (нет входящего остатка дебиторки,
-- дыры в выписках Озон Банка) — см. docs/reference/mp_receivable_recon_2026-09-22.md.
CREATE TABLE IF NOT EXISTS cf_mp_balance (
    account   text          NOT NULL,           -- wb_acc1/oz_acc2/ya_acc1
    day       date          NOT NULL,           -- дата снимка
    platform  text          NOT NULL,           -- wb / ozon / yandex
    org_inn   text          NOT NULL,
    balance   numeric(14,2) NOT NULL,
    source    text          NOT NULL,           -- каким методом получено (для доверия к цифре)
    loaded_at timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (account, day)
);

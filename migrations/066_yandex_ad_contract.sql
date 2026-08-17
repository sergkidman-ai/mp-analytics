-- поток: fin
-- Реклама Маркета по ОТДЕЛЬНОМУ договору на размещение (например 354817/19).
-- Этих начислений нет ни в одном отчёте Партнёр-API (проверено 17.08.2026: единый отчёт услуг
-- united-marketplace-services и отчёт взаиморасчётов united-netting за март-2026 суммы не
-- содержат — договор биллится вне businessId). Поэтому суммы вводятся руками из ЛК/актов
-- и живут здесь; коллектор добавляет их в строку «Продвижение» и показывает под-строкой.
CREATE TABLE IF NOT EXISTS yandex_ad_contract (
    account    text        NOT NULL,
    month      date        NOT NULL,           -- YYYY-MM-01
    contract   text        NOT NULL,           -- номер договора, напр. '354817/19'
    amount     numeric(14,2) NOT NULL,         -- расход за месяц, ₽ (положительное число)
    note       text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, month, contract)
);

-- Итог по договорам за месяц — в витрину, чтобы отчёт мог показать под-строку.
ALTER TABLE yandex_finance_monthly
    ADD COLUMN IF NOT EXISTS ad_contract numeric(14,2) NOT NULL DEFAULT 0;

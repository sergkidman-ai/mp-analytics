-- 217_supplier_balance_adjust.sql — поток: inv. Ручная сверка баланса аванса с поставщиком.
--
-- Баланс поставщика (метод prepayment_balance) считается на лету из МС: Σ неизрасходованных
-- остатков авансовых платежей, записанных из банковской выписки (alfa_link.advance_balance).
-- Отдельного гроссбуха не заводим (принцип 2 ARCHITECTURE.md) — здесь живёт ТОЛЬКО поправка,
-- которую человек ставит по итогу сверки с поставщиком («у вас на нас числится X»).
--
-- Храним не абсолютное значение, а ДЕЛЬТУ (stated − computed на момент сверки): расчётная часть
-- продолжает жить своей жизнью (новые авансы и приёмки её двигают), а поправка держит разрыв,
-- пока его причину не устранили. Строки не перезаписываем — это журнал сверок, в расчёт идёт
-- последняя по checked_at.
CREATE TABLE IF NOT EXISTS supplier_balance_adjust (
    id         serial PRIMARY KEY,
    org_inn    text NOT NULL,              -- наше юрлицо (у каждого свой баланс у поставщика)
    inn        text NOT NULL,              -- поставщик
    checked_at timestamptz NOT NULL DEFAULT now(),
    computed   numeric NOT NULL,           -- ₽, наш расчёт по МС на момент сверки
    stated     numeric NOT NULL,           -- ₽, факт по сверке с поставщиком
    delta      numeric NOT NULL,           -- ₽, stated − computed: прибавляется к расчёту
    author     text,
    note       text
);

CREATE INDEX IF NOT EXISTS idx_supplier_balance_adjust_key
    ON supplier_balance_adjust (org_inn, inn, checked_at DESC);

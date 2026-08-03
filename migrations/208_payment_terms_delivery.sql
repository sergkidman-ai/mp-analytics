-- 208_payment_terms_delivery.sql — поток: inv. Срок доставки поставщика в условиях оплаты.
--
-- Зачем: плановая дата приёмки в заказе МС считалась одинаково для всех — ближайший рабочий
-- день после счёта. У части поставщиков товар приезжает через день (Блоссом, Колортек), и
-- дату приходилось править руками; срок был зашит в код (`SUPPLIERS[inn]["plan_skip"]`),
-- то есть менялся только правкой файла. Срок — свойство ПОСТАВЩИКА, ему место рядом с
-- условиями оплаты, где владелец правит его сам.
--
-- Семантика (формулировка Сергея 03.08.2026): значение = через сколько РАБОЧИХ дней приёмка.
--   1 = 1 рабочий день, приёмка завтра  (счёт Пн → приёмка Вт);
--   2 = через 1 рабочий день, послезавтра (счёт Пн → приёмка Ср).
-- Выходные и праздники пропускаются: считает `invoice_bot/workcal.py` (skip = delivery_days − 1).
-- NULL здесь не нужен: срок есть всегда, по умолчанию 1 — прежнее поведение для всех.

ALTER TABLE supplier_payment_terms ADD COLUMN IF NOT EXISTS delivery_days smallint NOT NULL DEFAULT 1;

ALTER TABLE supplier_payment_terms DROP CONSTRAINT IF EXISTS supplier_payment_terms_delivery_days_ck;
ALTER TABLE supplier_payment_terms ADD CONSTRAINT supplier_payment_terms_delivery_days_ck
    CHECK (delivery_days BETWEEN 1 AND 30);

COMMENT ON COLUMN supplier_payment_terms.delivery_days IS
    'Срок доставки в РАБОЧИХ днях от даты счёта: 1 = приёмка завтра, 2 = послезавтра. '
    'Пропускает выходные и праздники (workcal). Источник правды для плановой даты приёмки заказа МС';

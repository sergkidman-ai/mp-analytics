-- поток: inv
-- Аренда платится ВПЕРЁД (решение Сергея 04.09.2026).
--
-- Было: первый понедельник месяца → платёж ЗА ЭТОТ ЖЕ месяц, то есть деньги приходили
-- арендодателю уже внутри оплачиваемого периода — просрочка по договору.
-- Стало: последний рабочий день месяца → платёж ЗА СЛЕДУЮЩИЙ месяц.
--
-- Почему «последний рабочий», а не «последнее число»: 30–31-е регулярно выходной, банк проведёт
-- платёж уже в следующем месяце — ровно та просрочка, от которой уходим. Рабочий день считает
-- invoice_bot/workcal.py (isdayoff.ru: праздники и переносы).

ALTER TABLE rent_plan DROP CONSTRAINT IF EXISTS rent_plan_pay_day_check;
UPDATE rent_plan SET pay_day = 'last_working_day', updated_at = now()
 WHERE pay_day <> 'last_working_day';
ALTER TABLE rent_plan ALTER COLUMN pay_day SET DEFAULT 'last_working_day';
ALTER TABLE rent_plan ADD CONSTRAINT rent_plan_pay_day_check
    CHECK (pay_day IN ('last_working_day'));

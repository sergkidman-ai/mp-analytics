-- 033: незаборы (невыкупы) Маркета в финансовой витрине.
-- Статусы отмен в stats/orders: CANCELLED_BEFORE_PROCESSING / CANCELLED_IN_PROCESSING /
-- CANCELLED_IN_DELIVERY (последний = незабор: заказ ехал, покупатель не забрал).
-- Ловушка: фильтр status='CANCELLED' не матчил НИ ОДИН из них — отменённые попадали
-- в выручку (их субсидии!) и в счётчик заказов. У незаборов есть реальные удержания
-- (логистика) — это расход, учитываем и показываем отдельно.

ALTER TABLE yandex_finance_monthly ADD COLUMN IF NOT EXISTS unredeemed_orders integer;
ALTER TABLE yandex_finance_monthly ADD COLUMN IF NOT EXISTS unredeemed_cost numeric;

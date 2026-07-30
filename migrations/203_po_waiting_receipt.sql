-- поток: inv
-- 203: статус 'waiting_receipt' в po_payment_status — гейт приёмки для метода «отсрочка».
--
-- Зачем: по отсрочке платим за ПРИНЯТЫЙ товар. Заказ может быть проведён (applicable=true),
-- а приёмка по нему висеть черновиком — товар фактически не принят, платить нельзя.
-- Мера приёмки — purchaseorder.shippedSum (считает только ПРОВЕДЁННЫЕ приёмки).
-- Такие заказы держим в 'waiting_receipt': в выборку 'pending' (из которой собирается пачка)
-- они не попадают, но видны в очереди как ожидание. Проведут приёмку — вернутся в 'pending'
-- на сумму принятого. Для предоплаты гейт не действует (платёж идёт ДО приёмки).

ALTER TABLE po_payment_status DROP CONSTRAINT IF EXISTS po_payment_status_status_check;
ALTER TABLE po_payment_status ADD CONSTRAINT po_payment_status_status_check
    CHECK (status IN ('pending', 'queued', 'paid', 'waiting_receipt'));

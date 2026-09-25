-- 225 (inv): хостинг «Бегет» — счёт из почты сразу в черновик платёжки.
-- Счёт приходит нерегулярно (когда кончается баланс хостинга) и оплачивается в три банковских
-- дня, поэтому ждать суточного прогона автоотправки нельзя: движок `invoice_bot/beget_invoice.py`
-- ставит платёж и увозит его в банк в тот же момент, когда письмо прочитано. Решение Сергея
-- 25.09.2026: «настроить создание черновика платёжки для Цифрового сразу после поступления счёта».
--
--   hosting — оплата хостинга/услуг связи по счёту из почты (папка «Бегет»).
-- Заказа поставщику в МойСкладе у такого платежа нет, как и у аренды: назначение задано текстом,
-- covers_po_ids пуст.
ALTER TABLE payment_draft_queue DROP CONSTRAINT IF EXISTS payment_draft_queue_kind_check;
ALTER TABLE payment_draft_queue ADD CONSTRAINT payment_draft_queue_kind_check
    CHECK (kind IN ('deferred_batch', 'prepayment_order', 'advance', 'rent', 'rent_utility', 'hosting'));

-- Порог суммы счёта. Таблица общая с коммунальными счетами: порог — свойство ПОЛУЧАТЕЛЯ,
-- а не вида услуги. Счета Бегета были 5 880 ₽ (26.06.2026) и 7 780 ₽ (22.09.2026); 20 000 ₽
-- ловит ошибку на порядок и при этом не встаёт на пути обычного пополнения.
INSERT INTO rent_utility_guard (payee_inn, max_amount) VALUES ('7801451618', 20000)
    ON CONFLICT (payee_inn) DO UPDATE SET max_amount = EXCLUDED.max_amount, updated_at = now();

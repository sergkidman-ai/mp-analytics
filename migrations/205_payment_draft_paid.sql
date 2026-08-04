-- поток: inv
-- 205: статус 'paid' в payment_draft_queue — по черновику деньги реально ушли.
--
-- Зачем: 'sent_prod' говорит только «отправлено в банк», а дальше платёж живёт своей жизнью
-- (владелец подписывает, банк проводит). Факт оплаты приезжает из МС: утренняя выписка Альфы
-- разносится в paymentin/paymentout, привязка закрывает payedSum заказа, поллер снимает заказ
-- в 'paid' — и когда оплачены ВСЕ заказы черновика, сам черновик тоже 'paid'
-- (po_payment_watch.mark_paid_drafts). Отличается от 'cancelled' смыслом: 'cancelled' — снят,
-- платить не будем; 'paid' — заплачено (в т.ч. вручную владельцем мимо системы).

ALTER TABLE payment_draft_queue DROP CONSTRAINT IF EXISTS payment_draft_queue_status_check;
ALTER TABLE payment_draft_queue ADD CONSTRAINT payment_draft_queue_status_check
    CHECK (status IN ('planned', 'sent_sandbox', 'sent_prod', 'error', 'cancelled', 'paid'));

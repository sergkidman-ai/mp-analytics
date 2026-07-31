-- поток: inv
-- 204: статус 'cancelled' в payment_draft_queue — снятие черновика из очереди без потери следа.
--
-- Зачем: счета регулярно оплачивают вручную (владелец платит из веба банка). Такой черновик
-- платить уже нельзя, но и удалять строку нельзя — теряется история «что планировали».
-- 'cancelled' = снят из очереди; отправщик (alfa_payment_draft.send_planned) берёт только
-- status='planned', поэтому снятое в банк не уйдёт. Причина снятия — в note.

ALTER TABLE payment_draft_queue DROP CONSTRAINT IF EXISTS payment_draft_queue_status_check;
ALTER TABLE payment_draft_queue ADD CONSTRAINT payment_draft_queue_status_check
    CHECK (status IN ('planned', 'sent_sandbox', 'sent_prod', 'error', 'cancelled'));

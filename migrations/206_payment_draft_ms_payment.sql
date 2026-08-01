-- поток: inv
-- 206: ms_payment_id — платёж МойСклада, закрывший черновик аванса.
--
-- Зачем: под авансом нет заказов (covers_po_ids пуст), поэтому «проведён» по нему определяется
-- не через payedSum, а поиском самого платежа: paymentout нашей организации этому контрагенту
-- на ту же сумму, начиная с даты черновика (платежи приезжают из выписки Альфы,
-- collectors/alfa_ms.py). Найденный платёж закрепляем за черновиком, иначе второй аванс той же
-- суммы тому же поставщику отметился бы тем же самым платежом.

ALTER TABLE payment_draft_queue ADD COLUMN IF NOT EXISTS ms_payment_id text;
CREATE INDEX IF NOT EXISTS payment_draft_queue_ms_payment_idx
    ON payment_draft_queue (ms_payment_id) WHERE ms_payment_id IS NOT NULL;

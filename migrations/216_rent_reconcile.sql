-- поток: inv
-- Закрытие арендных черновиков по ВЫПИСКЕ.
--
-- У обычного черновика статус меняет МойСклад: заказ закрылся оплатой (payedSum) либо под аванс
-- нашёлся paymentout. У аренды нет ни заказа, ни строки в `supplier_payment_terms` — по этому
-- пути арендный черновик не закроется никогда и будет вечно висеть в 'sent_prod'.
--
-- Источник правды для аренды — выписка: деньги ушли с нашего счёта арендодателю. Ссылку на
-- проводку держим в колонке ниже, чтобы одна проводка не закрыла два черновика на ту же сумму
-- (аренда и коммуналка одному и тому же получателю — обычное дело).

ALTER TABLE payment_draft_queue ADD COLUMN IF NOT EXISTS bank_txn_id bigint;
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_draft_queue_bank_txn
    ON payment_draft_queue (bank_txn_id) WHERE bank_txn_id IS NOT NULL;

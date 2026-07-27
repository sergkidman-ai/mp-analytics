-- 201_payment_terms_cap_agent.sql — поток: inv. Две колонки к supplier_payment_terms по
-- реальному файлу условий от заказчика («Сроки оплаты по поставщикам», 2026-07-27).
--
-- 1) payment_cap — «Размер предоплаты / оплаты по отсрочке» из файла: потолок ОДНОГО платежа
--    по поставщику. Для method='deferred' пачка неоплаченных заказов режется по
--    min(payment_cap, живой остаток на р/с). NULL = «вся сумма задолженности» (без потолка).
--    Для prepayment_balance размер аванса живёт в advance_amount (колонка не дублируется),
--    для prepayment_per_order платим счёт целиком → NULL.
-- 2) ms_agent_id — id карточки контрагента МойСклад. Нужен, потому что ИНН НЕ уникален в МС:
--    у 7806486149 две карточки («ООО "Солюшнс принт"» и «ООО "Солюшнс принт" МСК»), заказы
--    идут только во вторую. Без явного id поллер брал rows[0] и мог смотреть в пустую карточку.
--    NULL = резолвить по ИНН как раньше (когда карточка одна).

ALTER TABLE supplier_payment_terms ADD COLUMN IF NOT EXISTS payment_cap numeric;
ALTER TABLE supplier_payment_terms ADD COLUMN IF NOT EXISTS ms_agent_id text;

COMMENT ON COLUMN supplier_payment_terms.payment_cap IS
    'Потолок одного платежа, ₽ (deferred: кап пачки). NULL = вся сумма задолженности';
COMMENT ON COLUMN supplier_payment_terms.ms_agent_id IS
    'id карточки контрагента МойСклад (ИНН не уникален). NULL = резолвить по ИНН';

-- 202_payment_terms_vat.sql — поток: inv. Ставка НДС поставщика в условиях оплаты.
--
-- Зачем: назначение платежа обязано содержать НДС-оговорку, и до сих пор она выводилась из
-- заказа МС (`vatEnabled`/`vatSum`). Это ненадёжно — у аванса заказа нет вообще, и платёж
-- уходил с «Без НДС», хотя поставщик на ОСНО (владелец в ручных авансах пишет «В том числе
-- НДС 22%, 9016.39 руб.»). Ставка — свойство ПОСТАВЩИКА, а не отдельного документа, поэтому
-- держим её здесь и проверяем глазами один раз.
--
-- Семантика: 22 = ставка 22%, 0 = «НДС не облагается» (УСН и т.п.), NULL = не заполнено —
-- тогда платёжка откатывается на прежний вывод НДС из заказа МС.

ALTER TABLE supplier_payment_terms ADD COLUMN IF NOT EXISTS vat_rate integer;

ALTER TABLE supplier_payment_terms DROP CONSTRAINT IF EXISTS supplier_payment_terms_vat_rate_ck;
ALTER TABLE supplier_payment_terms ADD CONSTRAINT supplier_payment_terms_vat_rate_ck
    CHECK (vat_rate IS NULL OR vat_rate BETWEEN 0 AND 100);

COMMENT ON COLUMN supplier_payment_terms.vat_rate IS
    'Ставка НДС поставщика, %: 22 = 22%, 0 = НДС не облагается, NULL = не задано (НДС из заказа МС)';

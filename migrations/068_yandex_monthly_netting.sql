-- поток: fin
-- Витрина Маркета: зачёт баллов по месяцам, чтобы показать оборот и удержания ГРОСС —
-- в одной форме с Ozon («Баллы за скидки за счёт Озон») и ВБ («СПП за счёт ВБ»).
--
-- ОБОРОТ-ГРОСС = revenue + netting, а НЕ revenue + subsidy. Обе части при этом взяты из актов
-- (деньги покупателя из closure, зачёт из отчёта услуг), поэтому «Итого к перечислению»
-- не меняется ни на рубль и по-прежнему сходится с ЛК. Субсидия по ЗАКАЗАМ (yandex_monthly.subsidy)
-- живёт в другом месяце: акт приходит с лагом (июнь: зачёт 909 098 при субсидии 730 154;
-- июль: зачёт 935 413 при субсидии 1 000 052). Она остаётся справочной строкой.
--
-- Разбивка по категориям нужна, чтобы каждую строку удержаний показать начисленной:
-- комиссия = fee + netting_fee, логистика = delivery + netting_delivery,
-- продвижение = promotion + netting_promotion (бусты + Полки). У эквайринга, прочего
-- и подписки зачёта не бывает (проверено по всей истории янв–авг 2026).
ALTER TABLE yandex_finance_monthly
    ADD COLUMN IF NOT EXISTS netting            numeric(14,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS netting_fee        numeric(14,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS netting_delivery   numeric(14,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS netting_promotion  numeric(14,2) DEFAULT 0;

COMMENT ON COLUMN yandex_finance_monthly.netting IS 'погашено баллами Маркета всего, ₽ (= вторая часть оборота-гросс)';

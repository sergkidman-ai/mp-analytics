-- 207_payment_terms_org.sql — поток: inv. Разрез по ЮРЛИЦУ в графике оплаты поставщикам.
--
-- Зачем: контур платежей перестал быть одноорганизационным. У ООО «ДИСКВЭР» (7811803918,
-- Сбер) свои заказы поставщикам, свои приёмки и СВОИ условия оплаты, при этом 9 из 11
-- поставщиков — те же, что у ООО «Цифровой Квадрат» (7807355364, Альфа). Ключ
-- supplier_payment_terms = один только ИНН поставщика физически не даёт завести вторые
-- условия: они затирают первые. Замер по истории МС (docs/reports/diskver_payment_terms.md)
-- показал реальные расхождения — Одиссей (у ЦК отсрочка 14, у Дисквэра фактически аванс),
-- Блоссом (4 → 17), КПД (5 → аванс).
--
-- Что делаем: org_inn = ИНН НАШЕГО юрлица-плательщика во всех трёх таблицах контура,
-- PK условий оплаты → (org_inn, inn). DEFAULT '7807355364' — все существующие строки это
-- контур Цифрового Квадрата, других на момент миграции не было.
--
-- ВАЖНО про безопасность: после этой миграции по одному ИНН поставщика возвращается ДВЕ
-- строки условий. Любой потребитель обязан фильтровать по org_inn, иначе задвоит черновики
-- платёжек (живые деньги). Гейты проставлены в po_payment_watch.run, alfa_payment_draft
-- (JOIN по (org_inn, inn)), detect_vat_rate.

ALTER TABLE supplier_payment_terms ADD COLUMN IF NOT EXISTS org_inn text NOT NULL DEFAULT '7807355364';
ALTER TABLE payment_draft_queue    ADD COLUMN IF NOT EXISTS org_inn text NOT NULL DEFAULT '7807355364';
ALTER TABLE po_payment_status      ADD COLUMN IF NOT EXISTS org_inn text NOT NULL DEFAULT '7807355364';

COMMENT ON COLUMN supplier_payment_terms.org_inn IS
    'ИНН нашего юрлица-плательщика: 7807355364 = Цифровой Квадрат (Альфа), 7811803918 = Дисквэр (Сбер)';
COMMENT ON COLUMN payment_draft_queue.org_inn IS 'ИНН нашего юрлица-плательщика';
COMMENT ON COLUMN po_payment_status.org_inn IS 'ИНН нашего юрлица-плательщика';

-- PK условий оплаты: (org_inn, inn). po_payment_status.po_id остаётся PK — id заказа в МС
-- глобально уникален, org_inn там атрибут для фильтра, а не часть ключа.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'supplier_payment_terms_pkey' AND contype = 'p') THEN
        ALTER TABLE supplier_payment_terms DROP CONSTRAINT supplier_payment_terms_pkey;
        ALTER TABLE supplier_payment_terms ADD PRIMARY KEY (org_inn, inn);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_po_payment_status_org ON po_payment_status (org_inn, inn, status);
CREATE INDEX IF NOT EXISTS idx_payment_draft_queue_org ON payment_draft_queue (org_inn, status);

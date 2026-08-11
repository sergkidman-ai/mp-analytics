-- поток: inv
-- Аренда и коммуналка арендодателя: два новых основания платежа в очереди черновиков.
--
-- Почему отдельные kind, а не 'deferred_batch': у арендных платежей нет заказа поставщику
-- в МойСкладе (covers_po_ids пуст), основание — договор аренды либо счёт арендодателя,
-- и назначение платежа задано текстом, а не собирается из номеров заказов.
--
--   rent          — постоянная арендная плата, первый понедельник месяца (rent_plan);
--   rent_utility  — компенсация коммунальных услуг по счёту из почты (папка «Аренда»).

ALTER TABLE payment_draft_queue DROP CONSTRAINT IF EXISTS payment_draft_queue_kind_check;
ALTER TABLE payment_draft_queue ADD CONSTRAINT payment_draft_queue_kind_check
    CHECK (kind IN ('deferred_batch', 'prepayment_order', 'advance', 'rent', 'rent_utility'));

-- Реквизиты получателя, взятые ИЗ СЧЁТА (решение Сергея 11.08.2026: «реквизиты получателя
-- взять из счета»). Карточка МС при этом остаётся контролем: расхождение = стоп, а не платёж
-- «куда-нибудь». NULL — прежний путь, реквизиты из МойСклада (payment_draft.payee_block).
ALTER TABLE payment_draft_queue ADD COLUMN IF NOT EXISTS payee jsonb;

-- Готовое назначение платежа. У аренды оно диктуется договором (номер, площадь, ставка НДС)
-- и повторяется дословно из месяца в месяц — собирать его из документов МС нечем и незачем.
ALTER TABLE payment_draft_queue ADD COLUMN IF NOT EXISTS purpose_text text;

-- Ключ идемпотентности: «этот платёж за этот период уже ставили в очередь».
--   rent          — 'rent:<org_inn>:<YYYY-MM>' (месяц аренды);
--   rent_utility  — 'util:<org_inn>:<номер счёта>' (номер счёта арендодателя).
-- Повторный запуск планировщика или повторно прочитанное письмо не плодят вторую платёжку
-- на те же деньги (правило идемпотентности CLAUDE.md №3).
ALTER TABLE payment_draft_queue ADD COLUMN IF NOT EXISTS idem_key text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_draft_queue_idem
    ON payment_draft_queue (idem_key) WHERE idem_key IS NOT NULL;


-- ── План постоянных платежей (аренда) ────────────────────────────────────────────────────────
-- Суммы и текст назначения живут в таблице, а не в коде: арендодатель меняет ставку письмом,
-- и это правка строки, а не деплой. Значения засеяны из фактических платежей февраля–августа
-- 2026 (выписки, bank_txn): у обоих юрлиц один арендодатель — АО «Курганмашзавод».
CREATE TABLE IF NOT EXISTS rent_plan (
    id              serial PRIMARY KEY,
    org_inn         text NOT NULL,           -- наше юрлицо-плательщик (выбор банка идёт по нему)
    payee_inn       text NOT NULL,           -- арендодатель
    amount          numeric NOT NULL,        -- ₽, постоянная составляющая
    purpose_tpl     text NOT NULL,           -- назначение; {month} → месяц аренды в род. падеже
    pay_day         text NOT NULL DEFAULT 'first_monday'
                    CHECK (pay_day IN ('first_monday')),
    active          boolean NOT NULL DEFAULT true,
    note            text,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_inn, payee_inn)
);

INSERT INTO rent_plan (org_inn, payee_inn, amount, purpose_tpl, note) VALUES
  ('7807355364', '4501008142', 56907.65,
   'Оплата за аренду помещения за {month}. В том числе НДС 22%, 10262.04 руб.',
   'ООО «Цифровой квадрат» → АО «Курганмашзавод», договор 2304/28 от 07.02.2025. '
   'Сумма неизменна с февраля 2026 (56907.65 в фев–авг).'),
  ('7811803918', '4501008142', 49448.11,
   'Оплата за аренду помещения по договору аренды помещения ком. №27 33,6 кв.м. за {month}. '
   'В том числе НДС 22 % - 8916.87 рублей.',
   'ООО «Дисквэр» → АО «Курганмашзавод», договор 2026/3 от 11.02.2026, ком. №27, 33,6 кв.м. '
   'Сумма 49448.11 в июн–авг (ранее 48821.14–49794.19 — уточнялась).')
ON CONFLICT (org_inn, payee_inn) DO NOTHING;


-- ── Предохранитель по сумме коммунального счёта ──────────────────────────────────────────────
-- Решение Сергея 11.08.2026: счёт дороже порога в банк НЕ уходит — алерт в TG и разбор руками.
-- Факт за фев–июль 2026: 2 994–5 168 ₽, то есть порог 10 000 ₽ = двойной запас от максимума.
CREATE TABLE IF NOT EXISTS rent_utility_guard (
    payee_inn   text PRIMARY KEY,
    max_amount  numeric NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO rent_utility_guard (payee_inn, max_amount) VALUES ('4501008142', 10000)
ON CONFLICT (payee_inn) DO NOTHING;

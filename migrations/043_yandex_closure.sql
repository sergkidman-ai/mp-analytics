-- 043: сырьё детализированного отчёта о схождении с закрывающими документами Яндекса.
-- Источник выручки и возвратов (эталон ЛК → Финансы → Закрывающие документы):
--   лист period_closure_income_payments  → category='revenue' (Получено от потребителей);
--   лист period_closure_income_refunds   → category='returns' (Возвращено потребителям, знак −).
-- Проверено на январе 2026 до копейки: revenue Σ=955629, returns Σ=−62018.
-- Заполняется collectors/yandex_closure.py (API POST /v2/reports/closure-documents/
-- detalization/generate, contractType=INCOME). Идемпотентно: снапшот на (account, ym),
-- дедуп по transaction_id (защита от задвоения между campaignId одного договора).

CREATE TABLE IF NOT EXISTS raw_yandex_closure (
    account          text        NOT NULL,
    ym               text        NOT NULL,          -- YYYY-MM отчётного месяца
    category         text        NOT NULL,          -- revenue | returns
    transaction_id   text        NOT NULL,
    transaction_date date,
    order_id         text,
    offer_id         text,
    offer_name       text,
    count            integer,
    amount           numeric     NOT NULL,          -- TRANSACTION_SUM (returns со знаком −)
    campaign_id      text,
    source           text        DEFAULT 'api',
    loaded_at        timestamptz DEFAULT now(),
    payload          jsonb,
    PRIMARY KEY (account, category, transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_ya_closure_acc_ym ON raw_yandex_closure (account, ym);

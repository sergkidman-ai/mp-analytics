-- 709 — уведомления о выкупе Wildberries и УПД по ним (конвертер для Диадока).
--
-- Зачем: выкупая наш товар, ВБ выпускает «Уведомление о выкупе» (documents-api,
-- category=redeem-notification). Юридически это основание передачи, и на каждое
-- уведомление продавец обязан выставить РВБ счёт-фактуру с документом об отгрузке
-- (УПД, КНД 1115131, функция СЧФДОП) и отправить через ЭДО. Руками это перебивание
-- таблицы из XLSX в XML — ровно та работа, которую делает конвертер.
--
-- ВБ отдаёт уведомление ТОЛЬКО zip'ом с XLSX внутри (плюс .sig и МЧД), поэтому
-- разобранные позиции храним сами: второй раз к API за ними не сходить бесплатно
-- (лимит 1 запрос в 10 секунд на продавца).

CREATE TABLE IF NOT EXISTS wb_redeem_notice (
    account       text NOT NULL,          -- wb_acc1 | wb_acc2
    doc_number    text NOT NULL,          -- номер уведомления, он же номер УПД
    doc_date      date NOT NULL,          -- дата уведомления (НЕ дата выкладки в ЛК)
    service_name  text NOT NULL,          -- redeem-notification-<номер>, ключ скачивания
    created_at    timestamptz,            -- creationTime: когда ВБ выложил документ
    total_wo_vat  numeric NOT NULL,       -- итоги пересчитаны из позиций, а не из строки
    total_vat     numeric NOT NULL,       -- «Итого» файла: строка «Итого» — для сверки
    total_with_vat numeric NOT NULL,
    positions     jsonb NOT NULL,         -- [{n, article, name, qty, sum_with_vat, vat_rate, vat_sum}]
    upd_status    text NOT NULL DEFAULT 'due',   -- due | made (УПД выгружен в Диадок)
    upd_made_at   timestamptz,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, doc_number)
);
CREATE INDEX IF NOT EXISTS wb_redeem_notice_date_idx ON wb_redeem_notice (doc_date);

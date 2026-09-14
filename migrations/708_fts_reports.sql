-- 708 — статистические формы ФТС по продажам в страны ЕАЭС (подраздел «Отчёты ФТС»).
--
-- Зачем: раз в месяц мы обязаны подать в таможню статформу по каждой отгрузке в ЕАЭС,
-- по которой маркетплейс НЕ выкупил товар у нас. Если выкупил (Ozon: отметка «Подлежит
-- выкупу») — собственником стал он, и отчитывается он же. Признак выкупа живёт только
-- в CSV-отчёте Ozon по заказам, ни в одном postings-методе его нет, поэтому отчёт
-- складываем сырьём. ВБ закрывает ЕАЭС сам, у Яндекса зарубежных заказов нет — но
-- ключи таблиц сделаны площадко-независимыми, чтобы это можно было изменить без миграции.

-- Сырой отчёт Ozon по заказам. Одна строка = одно отправление (в отчёте строка на товар,
-- при загрузке схлопываем в отправление, товарные строки лежат в payload->'items').
CREATE TABLE IF NOT EXISTS raw_ozon_orders_report (
    account         text NOT NULL,      -- oz_acc1 | oz_acc2
    posting_number  text NOT NULL,      -- «Номер отправления»
    delivery_schema text,               -- fbo | fbs (API принимает ровно одну схему за запрос)
    status          text,               -- «Статус» отчёта, нас интересует «Доставлен»
    delivered_at    date,               -- «Дата доставки» — по ней отбор в отчётный период,
                                        -- отгружено могло быть и месяцами раньше
    is_buyout       boolean,            -- «Выкуп товара» = «Подлежит выкупу»
    payload         jsonb NOT NULL,     -- отправление целиком + список товарных строк
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, posting_number)
);
CREATE INDEX IF NOT EXISTS raw_ozon_orders_report_delivered_idx
    ON raw_ozon_orders_report (delivered_at);

-- Курсы ЦБ на дату. Курс для статформы берётся на ДАТУ ДОСТАВКИ заказа, а не на дату
-- отгрузки и не на дату подачи. Кэшируем, чтобы не ходить на cbr.ru при каждой перерисовке.
CREATE TABLE IF NOT EXISTS cbr_rate (
    rate_date date    NOT NULL,
    code      text    NOT NULL,         -- KZT | BYN | AMD | KGS | USD
    nominal   integer NOT NULL,         -- курс дан за nominal единиц (KZT — за 100)
    rate      numeric NOT NULL,
    PRIMARY KEY (rate_date, code)
);

-- Статус подачи. Проставляется вручную кнопкой в дашборде: факт загрузки формы в ЛК ФТС
-- виден только человеку, автоматически его узнать неоткуда.
CREATE TABLE IF NOT EXISTS fts_posting_status (
    period         date NOT NULL,       -- первое число отчётного месяца
    platform       text NOT NULL,       -- ozon | wb | yandex
    account        text NOT NULL,
    posting_number text NOT NULL,
    status         text NOT NULL DEFAULT 'due',   -- due | filed
    filed_at       timestamptz,
    note           text,
    PRIMARY KEY (period, platform, account, posting_number)
);

-- ГТД и страна происхождения на позиции приёмки — источник номера ГТД для формы
-- (берём по FIFO: последняя приёмка этой номенклатуры до даты отгрузки).
ALTER TABLE ms_supply_pos ADD COLUMN IF NOT EXISTS gtd     text;
ALTER TABLE ms_supply_pos ADD COLUMN IF NOT EXISTS country text;

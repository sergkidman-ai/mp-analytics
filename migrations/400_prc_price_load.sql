-- поток: prc
-- Журнал загрузок прайсов поставщиков в МойСклад (Оприходование на «Удаленный склад»).
--
-- prc_price_load — шапка прогона: кто, когда, по какому курсу, что получилось.
-- prc_price_row  — снимок КАЖДОЙ строки прайса с решением загрузчика. Это и разбор
--                  полётов («почему этой позиции нет на остатке»), и вход для шага 2
--                  (новинки поставщика, которых нет в МС по артикулу).

CREATE TABLE IF NOT EXISTS prc_price_load (
    id            BIGSERIAL PRIMARY KEY,
    supplier_key  TEXT        NOT NULL,          -- ключ группы: colortek, rapid, ...
    load_date     DATE        NOT NULL,          -- дата, на которую грузим прайс
    moment        TIMESTAMPTZ NOT NULL,          -- фактическое время загрузки (МСК)
    source_file   TEXT,                          -- имя файла прайса
    source_kind   TEXT,                          -- mail | file
    currency      TEXT,
    rate          NUMERIC(20, 10),               -- курс пересчёта (ЦБ × надбавка), без округления
    rate_date     TEXT,                          -- дата курса по ответу ЦБ
    rows_total    INTEGER     NOT NULL DEFAULT 0,
    rows_loaded   INTEGER     NOT NULL DEFAULT 0,
    rows_skipped  INTEGER     NOT NULL DEFAULT 0,
    docs          INTEGER     NOT NULL DEFAULT 0,   -- создано оприходований
    stale_docs    INTEGER     NOT NULL DEFAULT 0,   -- удалено прошлых
    card_updates  INTEGER     NOT NULL DEFAULT 0,   -- обновлено карточек товара
    sum_rub       NUMERIC(18, 2),
    dry_run       BOOLEAN     NOT NULL DEFAULT TRUE,
    status        TEXT        NOT NULL DEFAULT 'ok',   -- ok | error
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS prc_price_load_supplier_idx
    ON prc_price_load (supplier_key, load_date DESC);

CREATE TABLE IF NOT EXISTS prc_price_row (
    load_id    BIGINT      NOT NULL REFERENCES prc_price_load (id) ON DELETE CASCADE,
    row_no     INTEGER     NOT NULL,             -- номер строки в файле прайса
    article    TEXT        NOT NULL,             -- артикул поставщика (ключ матчинга)
    name       TEXT,                             -- наименование из прайса
    stock_raw  TEXT,                             -- остаток как его написал поставщик
    qty        NUMERIC(14, 3),                   -- остаток после интерпретации
    price_src  NUMERIC(18, 4),                   -- цена из прайса в валюте прайса
    price_rub  NUMERIC(18, 2),                   -- цена после пересчёта в рубли
    ms_id      TEXT,                             -- карточка МС, если нашлась
    ms_name    TEXT,
    status     TEXT        NOT NULL,             -- loaded | skipped
    reason     TEXT,                             -- код причины пропуска
    PRIMARY KEY (load_id, row_no)
);

CREATE INDEX IF NOT EXISTS prc_price_row_article_idx ON prc_price_row (article);
CREATE INDEX IF NOT EXISTS prc_price_row_reason_idx  ON prc_price_row (reason)
    WHERE reason IS NOT NULL;

-- Товары, которые у поставщика в наличии, но в МС по артикулу не нашлись (или нашлись
-- неоднозначно) — по последней успешной загрузке каждого поставщика. Вход для шага 2:
-- новая модель либо известная модель, впервые появившаяся у этого поставщика.
CREATE OR REPLACE VIEW prc_unmatched AS
WITH last_load AS (
    SELECT DISTINCT ON (supplier_key) id, supplier_key, load_date, moment
    FROM prc_price_load
    WHERE status = 'ok'
    ORDER BY supplier_key, moment DESC
)
SELECT l.supplier_key,
       l.load_date,
       r.article,
       r.name,
       r.stock_raw,
       r.qty,
       r.price_rub,
       r.reason
FROM last_load l
JOIN prc_price_row r ON r.load_id = l.id
WHERE r.reason IN ('not_found', 'ambiguous');

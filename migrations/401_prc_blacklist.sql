-- поток: prc
-- Чёрный список артикулов: то, что уже побывало в «необработанных» и было забраковано
-- человеком. Смысл — не показывать одно и то же второй раз: после каждой загрузки прайса
-- список новинок чистится по этой таблице.
--
-- Ключ сравнения — НОРМАЛИЗОВАННЫЙ артикул (upper + trim): у поставщиков один и тот же код
-- гуляет регистром и пробелами, а ЧС ведётся руками.

CREATE TABLE IF NOT EXISTS prc_blacklist (
    article_norm text PRIMARY KEY,
    article      text        NOT NULL,          -- как было в исходном списке
    source       text,                          -- откуда приехал (имя файла)
    note         text,
    added_at     timestamptz NOT NULL DEFAULT now()
);

-- Новинки = ненайденное последнего прогона МИНУС чёрный список.
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
LEFT JOIN prc_blacklist b ON b.article_norm = upper(btrim(r.article))
WHERE r.reason IN ('not_found', 'ambiguous')
  AND b.article_norm IS NULL;

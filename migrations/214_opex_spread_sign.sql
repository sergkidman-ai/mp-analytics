-- поток: inv
-- 214: направление разнесения переезжает со СТАТЬИ на КОНКРЕТНЫЙ платёж (знак в поле «Мес.»).
--
-- Почему отменяем 213. Уточнение Сергея 04.08.2026: внутри статьи «Налоги и взносы» лежат
-- два разных налога — НДС относится к месяцу оплаты, а УСН платится за три предыдущих месяца.
-- Признак у статьи их не различает, значит решает человек на каждом платеже.
--
-- Новая механика: поле «Мес.» знаковое, хранить отдельный флаг у разметки не нужно —
-- направление уже вшито в bank_txn_opex.start_month (вью 209 считает доли вперёд от него):
--    N = 1   → весь платёж в месяц оплаты (умолчание, как было);
--    N = 12  → вперёд: месяц оплаты и следующие 11 (лицензия на год);
--    N = −3  → назад: три месяца ДО оплаты (УСН, уплаченный в июле → апрель, май, июнь).
-- «Назад» распознаётся по данным: start_month < месяца платежа.
--
-- У ПРАВИЛА знак хранить надо — правило не помнит, у какого платежа оно родилось.

ALTER TABLE opex_rule ADD COLUMN IF NOT EXISTS spread_back BOOLEAN NOT NULL DEFAULT false;
COMMENT ON COLUMN opex_rule.spread_back IS
    'true = разносить назад: N месяцев, заканчивая месяцем ПЕРЕД платежом (налог за прошлый период)';

-- Признак у статьи (миграция 213) отменён: два механизма на одно и то же — источник расхождений.
-- Сначала возвращаем разнесённые им платежи в месяц оплаты — дальше Сергей разносит их руками.
UPDATE bank_txn_opex a
   SET start_month = date_trunc('month', t.operation_date)::date,
       updated_at  = now()
  FROM bank_txn t, opex_category c
 WHERE t.id = a.txn_id AND c.id = a.category_id AND c.spread_back
   AND a.start_month <> date_trunc('month', t.operation_date)::date;

ALTER TABLE opex_category DROP COLUMN IF EXISTS spread_back;

-- Вью выбора правила: добавляем знак в конец списка колонок (CREATE OR REPLACE это позволяет).
CREATE OR REPLACE VIEW opex_rule_match AS
SELECT DISTINCT ON (t.id)
       t.id            AS txn_id,
       r.id            AS rule_id,
       r.category_id,
       r.spread_months,
       r.spread_back
FROM bank_txn t
JOIN opex_rule r
  ON ((coalesce(t.cp_inn, '') <> '' AND r.cp_inn = t.cp_inn)
      OR (coalesce(t.cp_inn, '') = '' AND coalesce(r.cp_name_key, '') <> ''
          AND r.cp_name_key = regexp_replace(lower(coalesce(t.cp_name, '')),
                                             '[^0-9a-zа-яё]+', '', 'g')))
 AND (coalesce(r.purpose_like, '') = ''
      OR position(lower(r.purpose_like) in lower(coalesce(t.purpose, ''))) > 0)
 AND (r.amount_min IS NULL OR t.amount >= r.amount_min)
 AND (r.amount_max IS NULL OR t.amount <  r.amount_max)
JOIN opex_rule_reach w ON w.rule_id = r.id
WHERE t.direction = 'DEBIT'
ORDER BY t.id, w.n_match ASC, length(coalesce(r.purpose_like, '')) DESC, r.id DESC;

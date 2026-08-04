-- поток: inv
-- 211: вью «какое правило выигрывает у платежа».
--
-- После миграции 210 у одного контрагента может быть несколько правил (общее + уточнённые
-- фрагментом назначения), и платёж подходит сразу под несколько. Победитель один — самое
-- специфичное правило. Раньше эта логика жила только в apply_rules, поэтому счётчик
-- «размечено правилом» задваивал платежи, а удаление общего правила снимало разметку
-- и у платежей уточнённого. Теперь победитель считается в одном месте.
--
-- Специфичность = НАСКОЛЬКО УЗКО правило бьёт, то есть сколько платежей оно вообще
-- накрывает. Длина фрагмента для этого не годится: «Покупка PURCHASE_CB в» (21 символ)
-- длиннее, чем «KOMUS», но накрывает все карточные покупки подряд, а «KOMUS» — только
-- закупку упаковки. При равном охвате тай-брейк — длинный фрагмент, затем свежее правило.

-- Охват правила: сколько расходов оно вообще матчит (пересчитывается на лету, база мелкая).
CREATE OR REPLACE VIEW opex_rule_reach AS
SELECT r.id AS rule_id, count(t.id) AS n_match
FROM opex_rule r
LEFT JOIN bank_txn t
  ON t.direction = 'DEBIT'
 AND ((coalesce(t.cp_inn, '') <> '' AND r.cp_inn = t.cp_inn)
      OR (coalesce(t.cp_inn, '') = '' AND coalesce(r.cp_name_key, '') <> ''
          AND r.cp_name_key = regexp_replace(lower(coalesce(t.cp_name, '')),
                                             '[^0-9a-zа-яё]+', '', 'g')))
 AND (coalesce(r.purpose_like, '') = ''
      OR position(lower(r.purpose_like) in lower(coalesce(t.purpose, ''))) > 0)
GROUP BY r.id;

COMMENT ON VIEW opex_rule_reach IS
    'Сколько расходов накрывает правило — мера его широты для выбора победителя';

CREATE OR REPLACE VIEW opex_rule_match AS
SELECT DISTINCT ON (t.id)
       t.id            AS txn_id,
       r.id            AS rule_id,
       r.category_id,
       r.spread_months
FROM bank_txn t
JOIN opex_rule r
  ON ((coalesce(t.cp_inn, '') <> '' AND r.cp_inn = t.cp_inn)
      OR (coalesce(t.cp_inn, '') = '' AND coalesce(r.cp_name_key, '') <> ''
          AND r.cp_name_key = regexp_replace(lower(coalesce(t.cp_name, '')),
                                             '[^0-9a-zа-яё]+', '', 'g')))
 AND (coalesce(r.purpose_like, '') = ''
      OR position(lower(r.purpose_like) in lower(coalesce(t.purpose, ''))) > 0)
JOIN opex_rule_reach w ON w.rule_id = r.id
WHERE t.direction = 'DEBIT'
ORDER BY t.id, w.n_match ASC, length(coalesce(r.purpose_like, '')) DESC, r.id DESC;

COMMENT ON VIEW opex_rule_match IS
    'Платёж → выигравшее правило разметки (самое специфичное по фрагменту назначения)';

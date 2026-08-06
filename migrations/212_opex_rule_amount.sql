-- поток: inv
-- 212: диапазон суммы в правиле разметки.
--
-- Случай Казначейства (ИНН 7727406020): платит один контрагент, назначение у всех одинаковое
-- («Единый налоговый платеж»), но по смыслу это разные статьи — мелкие платежи это налоги ФОТ,
-- крупные — налоги и взносы. Ни ИНН, ни фрагмент назначения их не различают, различает только
-- сумма (решение Сергея 04.08.2026: порог 40 000 ₽).
--
-- Границы: amount_min ВКЛЮЧИТЕЛЬНО, amount_max ИСКЛЮЧИТЕЛЬНО — полуинтервал [min, max).
-- Так пара правил «до 40000» и «от 40000» покрывает всю прямую без дыры и без пересечения,
-- а ровно 40 000 ₽ попадает в верхнее правило (как и сказано: «менее 40 000 — ФОТ»).
-- NULL с любой стороны = граница не задана.

ALTER TABLE opex_rule ADD COLUMN IF NOT EXISTS amount_min NUMERIC;
ALTER TABLE opex_rule ADD COLUMN IF NOT EXISTS amount_max NUMERIC;

COMMENT ON COLUMN opex_rule.amount_min IS 'нижняя граница суммы платежа, включительно (NULL = нет)';
COMMENT ON COLUMN opex_rule.amount_max IS 'верхняя граница суммы платежа, исключительно (NULL = нет)';

-- Уникальность правила теперь «контрагент + фрагмент + диапазон»: у одного ИНН должно быть
-- РАЗРЕШЕНО несколько правил с разными порогами. coalesce — потому что NULL в уникальном
-- индексе не равен сам себе, и без него два правила «до 40000» ушли бы дублями.
DROP INDEX IF EXISTS uq_opex_rule_inn;
DROP INDEX IF EXISTS uq_opex_rule_name;

CREATE UNIQUE INDEX IF NOT EXISTS uq_opex_rule_inn
    ON opex_rule (cp_inn, (coalesce(lower(purpose_like), '')),
                  (coalesce(amount_min, -1)), (coalesce(amount_max, -1)))
    WHERE coalesce(cp_inn, '') <> '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_opex_rule_name
    ON opex_rule (cp_name_key, (coalesce(lower(purpose_like), '')),
                  (coalesce(amount_min, -1)), (coalesce(amount_max, -1)))
    WHERE coalesce(cp_name_key, '') <> '';

-- Вью выбора правила пересобираем целиком (миграция 211): к матчу добавляется диапазон.
-- Победителя по-прежнему определяет ОХВАТ — правило с порогом накрывает меньше платежей,
-- чем общее правило того же контрагента, поэтому выигрывает автоматически.
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
 AND (r.amount_min IS NULL OR t.amount >= r.amount_min)
 AND (r.amount_max IS NULL OR t.amount <  r.amount_max)
GROUP BY r.id;

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
 AND (r.amount_min IS NULL OR t.amount >= r.amount_min)
 AND (r.amount_max IS NULL OR t.amount <  r.amount_max)
JOIN opex_rule_reach w ON w.rule_id = r.id
WHERE t.direction = 'DEBIT'
ORDER BY t.id, w.n_match ASC, length(coalesce(r.purpose_like, '')) DESC, r.id DESC;

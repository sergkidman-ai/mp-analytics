-- поток: inv
-- Recovery databases could retain 210-era indexes after adding 212 columns/views.
-- The UI upserts on payee + purpose + amount bounds; restore that exact identity.
-- No rules or assignments are deleted; DDL is transactional (db.apply_sql_file).
DROP INDEX IF EXISTS uq_opex_rule_inn;
DROP INDEX IF EXISTS uq_opex_rule_name;
CREATE UNIQUE INDEX uq_opex_rule_inn
    ON opex_rule (cp_inn, (coalesce(lower(purpose_like), '')),
                  (coalesce(amount_min, -1)), (coalesce(amount_max, -1)))
    WHERE coalesce(cp_inn, '') <> '';
CREATE UNIQUE INDEX uq_opex_rule_name
    ON opex_rule (cp_name_key, (coalesce(lower(purpose_like), '')),
                  (coalesce(amount_min, -1)), (coalesce(amount_max, -1)))
    WHERE coalesce(cp_name_key, '') <> '';

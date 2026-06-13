-- migrations/002_products_keys.sql — Этап 1.
-- Сохраняем «родные» идентификаторы МойСклада под их именами (к ним цепляется связь
-- с маркетплейсами): article (=МС article), code (=МС code), external_code (=МС externalCode).
-- code оказался НЕ уникален (в группе 0002: 0002cs×5, 0002sp×4, 0002ep×3 …),
-- поэтому PK переносим на ms_id (единственный уникальный ключ карточки). products пуста — безопасно.

ALTER TABLE products ADD COLUMN IF NOT EXISTS code TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS external_code TEXT;

ALTER TABLE products DROP CONSTRAINT IF EXISTS products_pkey;
ALTER TABLE products ALTER COLUMN article DROP NOT NULL;   -- article больше не ключ, может быть пуст
ALTER TABLE products ALTER COLUMN ms_id SET NOT NULL;
ALTER TABLE products ADD PRIMARY KEY (ms_id);

CREATE INDEX IF NOT EXISTS idx_products_article ON products(article);
CREATE INDEX IF NOT EXISTS idx_products_code ON products(code);
CREATE INDEX IF NOT EXISTS idx_products_external_code ON products(external_code);

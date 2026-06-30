-- 014: каталог товаров Ozon (имя + флаг архива) по sku — для рейтинга карточек.
-- Архивные карточки (is_archived/is_autoarchived) не выводим; живым безымянным даём имя.
CREATE TABLE IF NOT EXISTS ozon_product (
    account TEXT, sku TEXT, offer_id TEXT, name TEXT,
    is_archived BOOLEAN DEFAULT false,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, sku)
);

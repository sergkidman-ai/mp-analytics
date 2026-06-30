-- 011: звёздный рейтинг товаров Ozon (агрегат из отзывов).
CREATE TABLE IF NOT EXISTS ozon_rating (
    account TEXT, sku TEXT, avg_rating NUMERIC, reviews_count INT,
    r1 INT, r2 INT, r3 INT, r4 INT, r5 INT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account, sku)
);

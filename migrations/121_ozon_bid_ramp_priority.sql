-- поток: mkt
-- Приоритет разгона ставок: сначала карточки, стоящие на позициях 11-30 по живым запросам
-- (из зоны 31+ покупателей практически нет: 3 заказа на 682 тыс. спроса за июнь-август).
ALTER TABLE mkt_ozon_bid_ramp ADD COLUMN IF NOT EXISTS priority smallint NOT NULL DEFAULT 5;
ALTER TABLE mkt_ozon_bid_ramp ADD COLUMN IF NOT EXISTS note text;
CREATE INDEX IF NOT EXISTS idx_ramp_priority ON mkt_ozon_bid_ramp (account, priority, status);

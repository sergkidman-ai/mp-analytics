-- 107_tc_buy_price.sql — mkt-блок. Живая «восстановительная» себестоимость с нашей платформы
-- TheCartridge (thecartridge.ru /api/catalog/best). Цены динамичные → храним ИСТОРИЮ по дням.
-- ВТОРАЯ себестоимость рядом с FIFO из отгрузок МС (fin), НЕ замена: buy_price = «почём купим
-- сегодня» (для решений mkt), FIFO = факт для отчётности. Ключ платформы — external_code (=МС externalCode).
-- status: 'ok' = есть ЛУ и цена; 'no_lu' = ЛУ отсутствует в моменте (buy_price NULL, НЕ ноль).

CREATE TABLE IF NOT EXISTS tc_buy_price (
    captured_date date        NOT NULL,           -- день замера (МСК)
    external_code text        NOT NULL,           -- код платформы = МС externalCode
    buy_price     numeric,                         -- живая закупочная; NULL при status='no_lu'
    status        text        NOT NULL,            -- 'ok' | 'no_lu'
    captured_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (captured_date, external_code)
);

CREATE INDEX IF NOT EXISTS idx_tc_buy_price_code ON tc_buy_price (external_code, captured_date DESC);
CREATE INDEX IF NOT EXISTS idx_tc_buy_price_status ON tc_buy_price (captured_date, status);

-- Последняя известная цена по коду (для расчёта маржи-контроля и джойнов).
CREATE OR REPLACE VIEW tc_buy_price_latest AS
SELECT DISTINCT ON (external_code)
       external_code, captured_date, buy_price, status, captured_at
FROM tc_buy_price
ORDER BY external_code, captured_date DESC;

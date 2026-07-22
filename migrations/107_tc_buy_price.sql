-- 107_tc_buy_price.sql — mkt-блок. Живая «восстановительная» себестоимость с нашей платформы
-- TheCartridge (thecartridge.ru /api/catalog/best). Цены динамичные → храним ИСТОРИЮ по дням.
-- ВТОРАЯ себестоимость рядом с FIFO из отгрузок МС (fin), НЕ замена: buy_price = «почём купим
-- сегодня» (для решений mkt), FIFO = факт для отчётности. Ключ платформы — external_code (=МС externalCode).
-- status: 'ok' = платформа дала цену; 'no_price' = платформы нет цены закупки на код в моменте
--         (buy_price NULL, НЕ ноль — отдельный статус).

CREATE TABLE IF NOT EXISTS tc_buy_price (
    captured_date date        NOT NULL,           -- день замера (МСК)
    external_code text        NOT NULL,           -- код платформы = МС externalCode
    buy_price     numeric,                         -- живая закупочная; NULL при status='no_price'
    status        text        NOT NULL,            -- 'ok' | 'no_price'
    captured_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (captured_date, external_code)
);

CREATE INDEX IF NOT EXISTS idx_tc_buy_price_code ON tc_buy_price (external_code, captured_date DESC);
CREATE INDEX IF NOT EXISTS idx_tc_buy_price_status ON tc_buy_price (captured_date, status);

-- Статус на ПОСЛЕДНЮЮ дату замера по коду (есть/нет цены сегодня).
CREATE OR REPLACE VIEW tc_buy_price_latest AS
SELECT DISTINCT ON (external_code)
       external_code, captured_date, buy_price, status, captured_at
FROM tc_buy_price
ORDER BY external_code, captured_date DESC;

-- Последняя ИЗВЕСТНАЯ (не NULL) цена по коду — для фолбэка, когда сегодня no_price, а раньше цена была.
-- Цены динамичны, но старая закупка ближе к истине, чем «нет цены»; помечаем как stale + дата.
CREATE OR REPLACE VIEW tc_buy_price_last_known AS
SELECT DISTINCT ON (external_code)
       external_code, captured_date AS price_date, buy_price
FROM tc_buy_price
WHERE buy_price IS NOT NULL
ORDER BY external_code, captured_date DESC;

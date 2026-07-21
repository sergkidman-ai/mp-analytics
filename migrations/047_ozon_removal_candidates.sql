-- 046: кандидаты на вывоз со склада Ozon FBO. Пересобирается на каждый прогон (снимок на run_date).
-- Правила OR (rules): C12 = days_without_sales>=90 (платное/застой), C3 = одиночный НЕ чёрный
-- картридж-компонент набора (цвет из атрибута 9602), C4 = появился на стоке в последние ~14д уже
-- после старта истории или архивная карточка с остатком (инвентаризация/из архива — watch).
-- Обязательные поля для заявки в ЛК: warehouse (точное имя склада), offer_id (артикул), qty (кол-во).
CREATE TABLE IF NOT EXISTS ozon_removal_candidates (
    run_date DATE,
    account TEXT,
    warehouse TEXT,            -- точное название склада (из ozon_fbo_stock) — для заявки
    offer_id TEXT,             -- артикул (external_code) — для заявки
    sku TEXT,
    name TEXT,
    qty INTEGER,               -- free_to_sell — количество к вывозу
    color TEXT,                -- атрибут 9602 «Цвет тонера»
    days_without_sales INTEGER,
    in_sets TEXT,              -- наборы, куда входит компонент (C3), через запятую
    first_seen DATE,           -- когда offer впервые замечен на стоке (для C4)
    is_archived BOOLEAN,
    rules TEXT,                -- какие правила сработали, напр. "C12,C3"
    PRIMARY KEY (run_date, account, warehouse, offer_id)
);
CREATE INDEX IF NOT EXISTS idx_ozon_removal_run ON ozon_removal_candidates(run_date, account);

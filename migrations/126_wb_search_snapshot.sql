-- поток: mkt
-- Публичная поисковая выдача WB по нашим моделям (docs/prompts/mkt_wb_scraper_start.md, этап 1).
-- Append-only лог: один снимок = один прогон collectors/wb_search_scrape.py, без апдейта задним
-- числом — сравнение продавца во времени идёт по captured_at, а не по перезаписи строки.
CREATE TABLE IF NOT EXISTS wb_search_snapshot (
    id            bigserial   PRIMARY KEY,
    run_id        uuid        NOT NULL,          -- один прогон скрипта = один run_id
    captured_at   timestamptz NOT NULL DEFAULT now(),
    query         text        NOT NULL,          -- поисковая строка (модель/код)
    region        text        NOT NULL,          -- метка региона (dest сверяем в скрипте)
    dest          bigint      NOT NULL,
    position      int         NOT NULL,          -- место в первой странице выдачи, 1..100
    nm_id         bigint      NOT NULL,
    supplier_id   bigint,
    supplier      text,
    brand         text,
    name          text,
    price         numeric,                       -- цена после СПП, ₽ (sizes[0].price.product/100)
    price_basic   numeric,                        -- цена до скидок, ₽
    rating        numeric,
    feedbacks     int,
    in_stock      boolean     NOT NULL,
    total_qty     int
);

CREATE INDEX IF NOT EXISTS wb_search_snapshot_query_idx ON wb_search_snapshot (query, region, captured_at);
CREATE INDEX IF NOT EXISTS wb_search_snapshot_nm_idx ON wb_search_snapshot (nm_id, captured_at);
CREATE INDEX IF NOT EXISTS wb_search_snapshot_run_idx ON wb_search_snapshot (run_id);

COMMENT ON TABLE wb_search_snapshot IS
  'Первая страница публичной выдачи search.wb.ru по нашим моделям — append-only снимки по времени.';

-- поток: mkt
-- Этап 2 из docs/prompts/mkt_wb_scraper_start.md: «тот же товар» — разбор названия строки выдачи
-- (wb_search_snapshot.name) на признаки, по которым сравнивать ТОЛЬКО сопоставимые позиции.
-- Замер 21.09 по живым данным: запрос «321» в выдаче — не только картриджи (батарейки, варочные
-- панели, металлопрокат совпали по номеру совместимости/коду) — item_kind='other' режет мусор
-- ДО любого сравнения цены/позиции, иначе сигналы будут врать.
-- Отдельная таблица, не колонки в snapshot: раздел «сырьё / расчёт» — разбор пересчитывается
-- из уже собранного name без повторного похода в ВБ (data-layer.md, принцип 1).
CREATE TABLE IF NOT EXISTS wb_search_match (
    snapshot_id  bigint      PRIMARY KEY REFERENCES wb_search_snapshot(id),
    item_kind    text        NOT NULL DEFAULT 'cartridge',  -- cartridge/chip/drum/head/other
    is_bundle    boolean     NOT NULL DEFAULT false,        -- «набор/комплект/мультиупак» есть в name
    bundle_qty   int,                                       -- число нашли рядом; NULL = бандл есть, число не нашли
    color        text,                                      -- bk/c/m/y/multi — эвристика по name
    is_xl        boolean     NOT NULL DEFAULT false,         -- увеличенный объём (XL/X2/повышенный ресурс)
    is_our       boolean     NOT NULL DEFAULT false,         -- nm_id есть в нашем wb_price (любой аккаунт)
    parsed_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wb_search_match_kind_idx ON wb_search_match (item_kind);
CREATE INDEX IF NOT EXISTS wb_search_match_our_idx ON wb_search_match (is_our) WHERE is_our;

COMMENT ON TABLE wb_search_match IS
  'Разбор названия строки выдачи (этап 2 промта) — тип/бандл/цвет/XL/наш, для сравнения «того же товара».';

-- 707 — знания по брендам и сериям brand_notes (поток rev).
-- Зачем: движок ходил за этими фактами в веб и приносил их с ошибками, а контролёр (llm_routing.verify)
-- честно резал «одноразовый чип» и «гарантия на заправку не распространяется» как утверждения вне
-- входных данных — потому что во входных данных их и не было. Теперь это НАШИ входные данные:
-- источник manual (принёс Сергей 10.09.2026), а не догадка модели и не чужой сайт.
-- Приоритет источников в промпте: CARD_DATA > brand_notes > approved_answers > веб-факты.

CREATE TABLE IF NOT EXISTS brand_notes (
    id             serial PRIMARY KEY,
    brand          text NOT NULL,                    -- hp | canon | … ; '*' — общее правило для всех брендов
    series_pattern text NOT NULL DEFAULT '',         -- серии/коды, к которым относится запись ('' = весь бренд)
    topic          text NOT NULL,                    -- чип | заправка | прошивка | серии | ресурс | …
    text           text NOT NULL,                    -- сам факт, как он уйдёт в промпт
    source         text NOT NULL DEFAULT 'manual',   -- manual — принесено человеком; иного пока нет
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (brand, topic, series_pattern)
);

CREATE INDEX IF NOT EXISTS brand_notes_brand_idx ON brand_notes (brand);

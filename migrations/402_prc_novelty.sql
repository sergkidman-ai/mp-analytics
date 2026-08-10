-- поток: prc
-- Новинки поставщиков и их разбор человеком.
--
-- Файл-отчёт хорош для чтения, но решение по строке принимается один раз и должно пережить
-- следующий прогон прайса: артикул придёт снова, а разбирать его второй раз незачем. Поэтому
-- строка живёт в таблице, а прогон лишь обновляет `last_seen` и список вариантов.
--
-- Ключ — поставщик + НОРМАЛИЗОВАННЫЙ артикул (upper + trim), как в prc_blacklist: у поставщиков
-- один и тот же код гуляет регистром и пробелами.

CREATE TABLE IF NOT EXISTS prc_novelty (
    id           bigserial PRIMARY KEY,
    supplier_key text        NOT NULL,
    article_norm text        NOT NULL,
    article      text        NOT NULL,
    name         text        NOT NULL,
    kind         text,                           -- cartridge | toner | ink (novelty.kind)
    color        text,                           -- BK | C | M | Y | ... (features.color)
    measure      numeric,                        -- ресурс печати, для флаконов — объём г/мл
    chip         text,                           -- chip | chip_free | nochip | NULL
    price_rub    numeric,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),

    -- Решение человека. pending — ещё не разобрано; matched — это наш товар, код в ms_code;
    -- new — полностью новая модель, заводим карточку; skip — не берём.
    decision     text        NOT NULL DEFAULT 'pending',
    ms_code      text,                           -- код товара в МС (6058sk и т.п.)
    ms_id        text,
    ms_name      text,
    decided_at   timestamptz,
    UNIQUE (supplier_key, article_norm)
);

CREATE INDEX IF NOT EXISTS prc_novelty_decision_idx ON prc_novelty (decision, supplier_key);

-- Варианты из нашего каталога: что сверка нашла по признакам (модель/цвет/ресурс/чип).
-- Пересобираются на каждом прогоне целиком — это не решение, а подсказка.
CREATE TABLE IF NOT EXISTS prc_novelty_candidate (
    novelty_id  bigint  NOT NULL REFERENCES prc_novelty(id) ON DELETE CASCADE,
    rank        int     NOT NULL,                -- 1 — лучший вариант
    ms_id       text    NOT NULL,
    ms_code     text,
    ms_name     text    NOT NULL,
    color       text,
    measure     numeric,
    chip        text,
    shared_code text,                            -- по какому коду сошлись
    verdict     text,                            -- «совпало по всем признакам» / чего не хватает
    PRIMARY KEY (novelty_id, rank)
);

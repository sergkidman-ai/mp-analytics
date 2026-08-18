-- поток: prc
-- Карточки МС без нашего внешнего кода, живущие в актуальных оприходованиях.
--
-- Внешний код (4 цифры) — ключ, по которому остаток уезжает из МойСклада дальше, в ТК.
-- Если карточка заведена мимо кода (в поле стоит автогенерация МС из 20+ символов), остаток
-- поставщика виден ТОЛЬКО внутри МС и в продажу не идёт. Аудит 18.08.2026 нашёл таких
-- 361 позицию на «Удаленном складе» (27 111 шт): Булат 207, ВТТ 96, дальше мелочь.
--
-- Почему отдельная таблица, а не `prc_novelty`. Новинка — это строка прайса, которой НЕТ в МС;
-- здесь наоборот: карточка есть, артикул поставщика на ней стоит, нет только кода. Через
-- новинки такая строка закрылась бы сама: `catalog.save()` ставит `decision='exists'` при
-- точном совпадении артикула с карточкой МС — а он совпадает всегда, на этой самой карточке.
-- Второй довод: у Булата, ВТТ, Рамис и Блоссома прайсы к нам не приходят (грузит внешний
-- загрузчик), их остаток живёт только в позициях оприходования, а не в `prc_price_row`.
--
-- Решение Сергея 17.08.2026: новые свободные коды этим карточкам НЕ выдавать — искать нашу
-- уже существующую карточку того же товара. Поэтому `target_*` — это НАША карточка, с которой
-- сводим, а не новый код.

CREATE TABLE IF NOT EXISTS prc_unlinked (
    ms_id        TEXT PRIMARY KEY,                  -- безкодовая карточка МС
    ms_code      TEXT,                              -- code МС (5586ct и т.п.), бывает пустым
    article      TEXT,                              -- артикул поставщика — он тут есть всегда
    name         TEXT        NOT NULL,
    supplier_key TEXT        NOT NULL,              -- группа из имени оприходования
    ext_raw      TEXT,                              -- что стоит в externalCode (автогенерация МС)
    archived     BOOLEAN     NOT NULL DEFAULT FALSE,
    category     TEXT,                              -- pathName папки; у 302 из 361 пусто
    qty          NUMERIC(14, 3),                    -- остаток из АКТУАЛЬНОГО оприходования
    docs         INTEGER     NOT NULL DEFAULT 0,    -- в скольких актуальных документах встретилась
    doc_name     TEXT,
    doc_date     DATE,
    store        TEXT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Решение человека. pending — не разобрано; matched — свели с нашей карточкой (target_*);
    -- new — нашей карточки действительно нет, вопрос заведения; skip — не наш товар/не берём.
    decision     TEXT        NOT NULL DEFAULT 'pending',
    target_ms_id TEXT,
    target_code  TEXT,                              -- внешний код нашей карточки (4 цифры)
    target_name  TEXT,
    decided_at   TIMESTAMPTZ,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS prc_unlinked_decision_idx ON prc_unlinked (decision, supplier_key);

-- Подсказки из нашего каталога: тот же матчинг, что у новинок (`prices/catalog.py`).
-- Пересобираются на каждом прогоне целиком — это не решение, а результат сегодняшней сверки.
CREATE TABLE IF NOT EXISTS prc_unlinked_candidate (
    ms_id         TEXT    NOT NULL REFERENCES prc_unlinked (ms_id) ON DELETE CASCADE,
    rank          INTEGER NOT NULL,                 -- 1 — лучший вариант
    cand_ms_id    TEXT    NOT NULL,
    cand_code     TEXT,
    external_code TEXT,                             -- наш 4-значный код кандидата
    cand_name     TEXT    NOT NULL,
    color         TEXT,
    measure       NUMERIC,
    chip          TEXT,
    brand         TEXT,
    kind          TEXT,
    shared_code   TEXT,                             -- по какому коду сошлись
    verdict       TEXT,
    model_ok      BOOLEAN,
    kind_ok       BOOLEAN,
    brand_ok      BOOLEAN,
    color_ok      BOOLEAN,
    resource_ok   BOOLEAN,
    chip_ok       BOOLEAN,
    score         NUMERIC,
    by_article    BOOLEAN NOT NULL DEFAULT FALSE,   -- совпал артикул поставщика, а не код модели
    feat_src      TEXT,
    PRIMARY KEY (ms_id, rank)
);

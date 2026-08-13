-- поток: prc
-- Слепок НАШЕГО каталога в TheCartridge — первоисточник характеристик товара.
--
-- До сих пор все шесть признаков сверки («строка прайса ↔ наш товар») разбирались из НАЗВАНИЙ
-- карточек МойСклада. Но МС — это товары поставщиков, заведённые руками: названия неполные,
-- местами ошибочные, и один внешний код несёт карточки всех поставщиков сразу. Первоисточник —
-- каталог в TheCartridge: одна карточка на внешний код, характеристики отдельными полями.
--
-- Замер 13.08.2026 по 6687 моделям и 127 строкам с подтверждённым человеком кодом:
--            цвет  ресурс  чип  бренд
--   МС (имя)  74%    57%   27%   92%
--   ТК (поля)100%    94%   16%  100%
-- То есть ТК не спорит с нами, а договаривает молчание (согласие там, где обе стороны
-- высказались: ресурс 92%, чип 97%, бренд 99%). Исключение — чип: его чаще знает МС.
--
-- Связь строго 1:1: у всех 6687 моделей внешний код заполнен и уникален.
-- В API не ходим на каждый разбор новинок: слепок обновляется ночным таймером.

CREATE TABLE IF NOT EXISTS prc_tc_model (
    external_code    text PRIMARY KEY,           -- он же ключ связи с ms_product.external_code
    title            text NOT NULL,              -- основное название модели (Q2610A)
    additional_title text,                       -- доп. название (10A) — синоним кода
    united_title     text,                       -- объединённое название
    similar_title    text,                       -- похожее название
    consumable_type  text,                       -- «Картридж», «Тонер», «Фотобарабан», …

    -- Разобранные признаки в НАШЕЙ кодировке (features.py), чтобы матчинг не разбирал заново.
    color            text,                       -- BK | C | M | Y | LC | LM | GY | …
    resource         integer,                    -- ресурс печати, страниц
    chip             text,                       -- chip | chip_free | nochip (NULL — не заполнено)
    brand            text[],                     -- бренды принтера, канонизированные

    printer_models   jsonb,                      -- [{brand, title}, …] как отдали
    weight_g         integer,
    height_mm        integer,
    width_mm         integer,
    depth_mm         integer,
    volume_ml        numeric,
    ved_code         text,                       -- ТН ВЭД ЕАЭС
    best_before_days integer,
    firmware_limit   text,

    raw              jsonb NOT NULL,             -- ответ API как есть: расчёт пересобирается из сырья
    first_seen       timestamptz NOT NULL DEFAULT now(),
    last_seen        timestamptz NOT NULL DEFAULT now(),
    gone_at          timestamptz                 -- пропала из выгрузки; строку НЕ удаляем
);

COMMENT ON TABLE prc_tc_model IS
    'Слепок каталога TheCartridge (POST /api/catalog/cartridge_models). Первоисточник '
    'характеристик товара; обновляется ночным таймером prc-tc-catalog, 34 запроса в сутки.';
COMMENT ON COLUMN prc_tc_model.gone_at IS
    'Модель перестала приходить в выгрузке. Не удаляем: исчезновение может быть и сбоем '
    'выгрузки, а карточки МС на этот внешний код продолжают жить.';

-- Индекс матчинга: нормализованный код -> внешний код. Строится коллектором через
-- features.codes() по всем четырём полям названия, чтобы матчер не разбирал названия
-- на каждом прогоне и мог искать одним SQL-запросом.
CREATE TABLE IF NOT EXISTS prc_tc_code (
    code          text NOT NULL,
    external_code text NOT NULL REFERENCES prc_tc_model(external_code) ON DELETE CASCADE,
    source        text NOT NULL,                 -- из какого поля названия пришёл код
    PRIMARY KEY (code, external_code)
);
CREATE INDEX IF NOT EXISTS prc_tc_code_code_idx ON prc_tc_code (code);

-- Вариант каталога теперь бывает двух сортов.
--   source='ms' — как раньше: карточка МойСклада (её код и уйдёт в решение);
--   source='tc' — товар нашёлся в каталоге ТК, а карточки МС под этим внешним кодом НЕТ.
-- Второй случай — подсказка человеку: заводить карточку надо под ЭТИМ внешним кодом, а не под
-- следующим свободным. Поэтому ms_id перестаёт быть обязательным.
ALTER TABLE prc_novelty_candidate
    ALTER COLUMN ms_id DROP NOT NULL;
ALTER TABLE prc_novelty_candidate
    ADD COLUMN IF NOT EXISTS source        text NOT NULL DEFAULT 'ms',
    ADD COLUMN IF NOT EXISTS external_code text,
    ADD COLUMN IF NOT EXISTS feat_src      text;

COMMENT ON COLUMN prc_novelty_candidate.source IS
    'ms — карточка МойСклада; tc — модель каталога ТК, у которой карточки МС нет (подсказка).';
COMMENT ON COLUMN prc_novelty_candidate.feat_src IS
    'Признаки, взятые из каталога ТК, через запятую (color,resource,chip,brand). '
    'Остальные разобраны из названия карточки МС.';

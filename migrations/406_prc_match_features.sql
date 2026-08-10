-- поток: prc
-- Шесть признаков сопоставления «строка прайса ↔ наша карточка» рядом и в явном виде.
--
-- Признаков было пять (модель, тип, цвет, ресурс/объём, чип) — бренда принтера в сверке
-- не было вовсе, хотя именно он ловит случайное совпадение кода модели («bizhub C250i»
-- и «Canon iR C250i»). Добавляем шестой и сохраняем результат сверки по каждому признаку,
-- а не только словесный вердикт: на этих флагах потом проверяется правило «6/6 → авто»,
-- задним числом по решениям человека, без нового прогона.
--
-- Флаг: true — признак подтвердился, false — противоречит, NULL — источник молчит
-- (в названии карточки признака нет; это «не знаем», а не «не совпало»).

ALTER TABLE prc_novelty
    ADD COLUMN IF NOT EXISTS brand text;                -- бренды принтера строки прайса

COMMENT ON COLUMN prc_novelty.brand IS
    'Бренды принтера из названия строки прайса, канонизированные и через запятую. '
    'Их может быть несколько: картридж подходит и к HP, и к Canon.';

ALTER TABLE prc_novelty_candidate
    ADD COLUMN IF NOT EXISTS brand       text,
    ADD COLUMN IF NOT EXISTS kind        text,
    ADD COLUMN IF NOT EXISTS model_ok    boolean,
    ADD COLUMN IF NOT EXISTS kind_ok     boolean,
    ADD COLUMN IF NOT EXISTS brand_ok    boolean,
    ADD COLUMN IF NOT EXISTS color_ok    boolean,
    ADD COLUMN IF NOT EXISTS resource_ok boolean,
    ADD COLUMN IF NOT EXISTS chip_ok     boolean,
    ADD COLUMN IF NOT EXISTS score       smallint;

COMMENT ON COLUMN prc_novelty_candidate.score IS
    'Сколько из шести признаков подтвердилось (true). NULL-флаги в счёт не идут: '
    'молчание каталога — не совпадение. Накопительная статистика под будущее правило 6/6.';

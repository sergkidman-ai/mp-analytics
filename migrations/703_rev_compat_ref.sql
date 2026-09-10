-- поток: rev — блок D брифа 08.09.2026: справочник совместимости «принтер → серия картриджа».
-- Причина: во всех четырёх разобранных провалах (Epson C5890 → T945x, Canon LBP646 → выдуманная
-- серия 075H, HP 4303 → выдуманная «спецификация HP 212A», Epson 680 → T026 вместо T017)
-- источником положительного ответа был веб или знание модели. Веб источником «да» быть перестаёт
-- (правило D1), а его место занимает эта таблица: в неё пишут только утверждённые человеком
-- ответы, ручная команда /compat_add и импорт OEM-списков. Веб-парсинг сюда НЕ пишет.
--
-- yield_variant — ресурсная версия серии (std/high/xl). Нужна отдельной колонкой, потому что
-- у OKI, Kyocera и HP LaserJet версии НЕ совместимы вниз: 12K-картридж в MB472 не встаёт.
-- region — рынок (EU/ASIA/US): у Epson WF-C5x90 европейские T11C/D/E и азиатские T11F/G — это
-- разные картриджи под один принтер, ответ без уточнения региона неверен по определению.
CREATE TABLE IF NOT EXISTS compat_ref (
    id               bigserial PRIMARY KEY,
    printer_model    text        NOT NULL,          -- НОРМАЛИЗОВАННАЯ модель принтера (compat_ref.norm)
    printer_raw      text,                          -- как её написал человек/источник — для карточки оператора
    cartridge_series text        NOT NULL,          -- наша серия/семейство картриджа
    oem_sku          text,                          -- код производителя, если известен
    yield_variant    text,                          -- std | high | xl | NULL
    region           text,                          -- EU | ASIA | US | NULL
    source           text        NOT NULL,          -- oem | approved_answer | manual
    approved_at      timestamptz NOT NULL DEFAULT now(),
    approved_by      text,
    note             text,
    CONSTRAINT compat_ref_source_ck CHECK (source IN ('oem', 'approved_answer', 'manual')),
    CONSTRAINT compat_ref_variant_ck CHECK (yield_variant IS NULL OR yield_variant IN ('std', 'high', 'xl')),
    CONSTRAINT compat_ref_region_ck CHECK (region IS NULL OR region IN ('EU', 'ASIA', 'US'))
);
-- Уникальность по брифу: (принтер, серия, вариант, регион). NULL в уникальном индексе не
-- сравнивается сам с собой, поэтому пустые вариант и регион сводим к '' явно.
CREATE UNIQUE INDEX IF NOT EXISTS compat_ref_key ON compat_ref
    (printer_model, cartridge_series, coalesce(yield_variant, ''), coalesce(region, ''));
CREATE INDEX IF NOT EXISTS compat_ref_printer_idx ON compat_ref (printer_model);
CREATE INDEX IF NOT EXISTS compat_ref_series_idx ON compat_ref (cartridge_series);

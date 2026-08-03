-- 063 — индекс совместимости «модель принтера → наши карточки» (поток rev).
-- Строится офлайн из названий/описаний/атрибутов листингов и вердиктов compat_cache
-- (reports/compat_index.py --build). Нужен, чтобы подбор товара в вопросах шёл ПО МОДЕЛИ
-- ПРИНТЕРА покупателя, а не по коду оригинального картриджа (которого у нас в названиях нет).

CREATE TABLE IF NOT EXISTS compat_index (
    platform    text NOT NULL,           -- wb | ozon | yandex (канал покупателя)
    account     text NOT NULL,           -- wb_acc1/wb_acc2/oz_acc1/oz_acc2/ya_acc1
    item_id     text NOT NULL,           -- то, что показываем покупателю: nm_id / Ozon SKU / marketSku
    article     text,                    -- наш код: vendor_code / offer_id
    title       text,
    url         text,                    -- витринная ссылка (нужна Яндексу, у WB/Ozon строится по id)
    item_kind   text NOT NULL,           -- toner|ink|drum|kit|ribbon|head|other
    brand       text,                    -- бренд ПРИНТЕРА из текста рядом с моделью (не наш бренд)
    model_core  text NOT NULL,           -- серия+цифры без суффикса: dcp7180
    model_norm  text NOT NULL,           -- серия+цифры+суффикс: dcp7180dn
    model_raw   text,                    -- как написано в источнике: DCP-7180DN
    src         text NOT NULL,           -- compat|title|descr|cache (приоритет в этом же порядке)
    verdict     text NOT NULL DEFAULT 'yes',   -- 'no' приходит из compat_cache и ГАСИТ пару товар×модель
    PRIMARY KEY (platform, item_id, model_norm, src)
);

CREATE INDEX IF NOT EXISTS compat_index_core_idx ON compat_index (model_core, platform);
CREATE INDEX IF NOT EXISTS compat_index_norm_idx ON compat_index (model_norm, platform);
CREATE INDEX IF NOT EXISTS compat_index_item_idx ON compat_index (platform, item_id);

-- Служебная строка о последней сборке (одна запись, id=1): когда собрано и сколько строк.
CREATE TABLE IF NOT EXISTS compat_index_meta (
    id          int PRIMARY KEY DEFAULT 1,
    built_at    timestamptz NOT NULL DEFAULT now(),
    rows_total  bigint NOT NULL DEFAULT 0,
    models_total bigint NOT NULL DEFAULT 0,
    items_total bigint NOT NULL DEFAULT 0,
    CONSTRAINT compat_index_meta_single CHECK (id = 1)
);

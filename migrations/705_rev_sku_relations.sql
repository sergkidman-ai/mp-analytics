-- 705 — связи номенклатуры sku_relations (поток rev).
-- Строится детерминированно из номенклатуры (reports/sku_relations.py --build), без LLM.
-- Зачем: на вопрос «а с чипом есть?», «что в комплекте?», «а тонер/барабан отдельно?» ответ должен
-- опираться на НАШУ номенклатуру, а не на догадку модели. Связь всегда внутри одного канала
-- покупателя — пара «площадка + аккаунт» (см. catalog._same_channel): лот Премиума покупателю
-- Дисквэра не поможет, это другой продавец.

CREATE TABLE IF NOT EXISTS sku_relations (
    platform     text NOT NULL,        -- wb | ozon | yandex
    account      text NOT NULL,        -- wb_acc1/wb_acc2/oz_acc1/oz_acc2/ya_acc1
    rel_type     text NOT NULL,        -- chip_pair | drum_toner | kit_component
    item_from    text NOT NULL,        -- артикул_от: id площадки (nm_id / Ozon SKU / marketSku)
    item_to      text NOT NULL,        -- артикул_к:  id площадки того же канала
    article_from text,                 -- наш внутренний код (offer_id / vendorCode) — для сверки
    article_to   text,
    kind_from    text,                 -- toner|ink|drum|kit|… на обоих концах: чем связь полезна
    kind_to      text,
    title_to     text,                 -- название и ссылка цели: ответу больше ничего не нужно
    url_to       text,
    ref_to       text,                 -- «Ozon SKU 123456» — то, что называем покупателю
    basis        text NOT NULL,        -- ПО ЧЕМУ связали: code=W1360A | models=3 | kit-code=…
    rank         int  NOT NULL DEFAULT 1,   -- 1 = лучший кандидат (их не больше трёх на связь)
    built_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, account, rel_type, item_from, item_to)
);

CREATE INDEX IF NOT EXISTS sku_relations_from_idx
    ON sku_relations (platform, account, item_from, rel_type, rank);

-- Паспорт последней сборки (одна строка, id=1): по нему видно протухание относительно compat_index.
CREATE TABLE IF NOT EXISTS sku_relations_meta (
    id          int PRIMARY KEY DEFAULT 1,
    built_at    timestamptz NOT NULL DEFAULT now(),
    rows_total  bigint NOT NULL DEFAULT 0,
    items_total bigint NOT NULL DEFAULT 0,
    CONSTRAINT sku_relations_meta_single CHECK (id = 1)
);

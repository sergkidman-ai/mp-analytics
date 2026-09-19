-- 126_wb_promo_plan.sql — поток: mkt — плановые цены акций ВБ из выгрузки ЛК.
--
-- Зачем файл, а не API: `calendar/promotions/nomenclatures` отдаёт плановую цену только для
-- акций type=regular; на автоакциях (наш случай) он отвечает 422 — проверено 10.09 и 19.09.2026.
-- Выгрузка из ЛК «Все товары, подходящие для акции» эту цену содержит.
--
-- Плановая цена ДЕРЖИТСЯ ВСЮ АКЦИЮ (Сергей, 19.09.2026), меняется только наше участие: цена
-- выросла выше плановой — товар из акции вышел. Поэтому файл грузится ОДИН раз, за сутки до
-- старта акции (бот присылает предупреждение), и живёт до конца акции.

create table if not exists wb_promo_plan_price (
    account      text        not null,
    nm_id        bigint      not null,
    vendor_code  text,                          -- артикул поставщика, в файле БЕЗ «/» (00011 = 0001/1)
    plan_price   numeric(12,2) not null,        -- «Плановая цена для акции» — потолок участия
    retail_price numeric(12,2),                 -- «Текущая розничная цена» на момент выгрузки
    disc_pct     numeric(6,2),                  -- «Текущая скидка на сайте, %»
    in_promo     boolean,                       -- «Товар уже участвует в акции»
    promo_name   text,
    valid_from   date        not null,          -- дата выгрузки
    valid_to     date,                          -- конец акции, если известен
    source_file  text,
    loaded_at    timestamptz not null default now(),
    primary key (account, nm_id, valid_from)
);

create index if not exists wb_promo_plan_price_live on wb_promo_plan_price (account, nm_id, valid_from desc);

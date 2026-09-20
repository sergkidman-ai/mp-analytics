-- 127_wb_promo_plan_bypromo.sql — поток: mkt
-- Плановая цена живёт НА АКЦИЮ, а карточка сидит в нескольких акциях сразу (19.09.2026:
-- 8 акций на acc1, 12 на acc2). Потолок подъёма = МИНИМУМ плановых цен по тем акциям, где
-- товар участвует: поднимешь выше — выпадет из соседней акции. Поэтому ключ таблицы
-- дополняется акцией.
alter table wb_promo_plan_price add column if not exists promo_key text;
update wb_promo_plan_price set promo_key = coalesce(promo_key, left(promo_name, 120)) where promo_key is null;
alter table wb_promo_plan_price alter column promo_key set not null;
alter table wb_promo_plan_price drop constraint if exists wb_promo_plan_price_pkey;
alter table wb_promo_plan_price add primary key (account, promo_key, nm_id, valid_from);

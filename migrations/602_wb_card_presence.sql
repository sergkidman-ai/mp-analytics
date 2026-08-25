-- поток: card — присутствие карточки WB в кабинете продавца
--
-- Зачем: `wb_cards` — накопительный слепок карточек, строки из него НЕ исчезают. Карточку,
-- удалённую в ЛК ВБ, коллектор просто перестаёт видеть, а строка живёт вечно и всплывает
-- во всех выборках как настоящая. Так 25.08.2026 в списке «Карточки без наличия» оказались
-- 7 мёртвых nm_id: витрина card.wb.ru их не отдаёт, рядом продаётся карточка-близнец того же
-- артикула с остатком. Проверка content-api показала: 5 удалены, 1 в корзине, 1 живая.
--
-- Узел новый, потому что менять чужую `wb_cards` (территория mkt/fin) поток card не вправе:
-- присутствие держим сбоку по тому же ключу account+nm_id и отдаём остальным через вьюху.

create table if not exists wb_card_presence (
    account       text        not null,               -- wb_acc1 | wb_acc2
    nm_id         bigint      not null,
    vendor_code   text,                               -- артикул на момент проверки (в ЛК меняется)

    in_cabinet    boolean     not null,               -- кабинет отдаёт карточку среди активных
    in_trash      boolean     not null default false, -- лежит в корзине ЛК (обратимо, 30 дней)

    first_missing timestamptz,                        -- когда ВПЕРВЫЕ не нашли (null = на месте)
    last_present  timestamptz,                        -- когда последний раз видели активной
    checked_at    timestamptz not null default now(),

    primary key (account, nm_id)
);

-- Основной вопрос к таблице — «кого больше нет»; таких единицы на 29 тыс. строк.
create index if not exists wb_card_presence_gone_idx
    on wb_card_presence (account, nm_id) where not in_cabinet;

comment on table wb_card_presence is
    'поток card: есть ли карточка WB в кабинете продавца. Пишет tools/card_wb_presence.py';

-- Готовый срез для остальных потоков: карточки WB, которые кабинет подтверждает.
-- Карточка, которую мы ещё ни разу не проверяли, считается живой (не режем данные молча).
create or replace view wb_cards_live as
select c.*,
       coalesce(p.in_cabinet, true) as in_cabinet,
       coalesce(p.in_trash, false)  as in_trash,
       p.first_missing
  from wb_cards c
  left join wb_card_presence p on p.account = c.account and p.nm_id = c.nm_id
 where coalesce(p.in_cabinet, true);

comment on view wb_cards_live is
    'поток card: wb_cards минус карточки, которых больше нет в кабинете ВБ (см. wb_card_presence)';

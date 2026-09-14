# Карта схемы Ozon в БД mp_analytics (снято 14.09.2026, read-only)

Схема `public`. Аккаунты: `oz_acc1` (Премиум-Про), `oz_acc2` (Дисквэр). Во всех Ozon-таблицах поле `account`,
в общих — ещё `platform='ozon'`. Время `timestamptz` хранится в UTC; «день» для постингов считать
`(in_process_at at time zone 'Europe/Moscow')::date`.

Ключи SKU:
- `sku` (Ozon SKU, число строкой; у одного товара несколько sku по источникам fbo/fbs) ;
- `offer_id` = наш артикул = `external_code` МойСклада (ведущий ноль — часть кода!);
- `margin_by_sku.article` для Ozon = **Ozon sku** (числовой), не offer_id;
- остатки МС ↔ Ozon: `supplier_stock.external_code = offer_id` (не `article`).

## 1. Сырьё (raw)

| Таблица | Гранулярность / ключ | Дата | Содержимое |
|---|---|---|---|
| `raw_ozon_transaction` | 1 строка = 1 финансовая операция; уникально `(account, operation_id)` | `payload->>'operation_date'` (`YYYY-MM-DD HH:MM:SS`, день операции); `period_from/period_to` = месяц сбора; `loaded_at` | `/v3/finance/transaction/list` как есть. payload: `operation_id, operation_type, operation_type_name, operation_date, type, amount` (сальдо операции), `accruals_for_sale` (>0 продажа по цене продавца, <0 возврат), `sale_commission, delivery_charge, return_delivery_charge`, `services[]{name,price}`, `items[]{sku,name}`, `posting{posting_number, order_date, delivery_schema FBS/FBO/RFBS, warehouse_id}`. Операции без `items[]` = расходы уровня аккаунта (реклама, Premium, эквайринг, звёздные и т.п.). Раскладка по статьям — `collectors/ozon.py::categorize_operation`, реестр строк — `ops/ozon_lines_registry.py`. История acc1 с 2025-02-01, acc2 с 2026-01-01. |
| `raw_ozon_posting` | 1 строка = отправление; уникально `(account, posting_number)` | `in_process_at` (момент заказа/принятия в обработку), `period_from/to` = месяц сбора, `loaded_at` | `/v3/posting/fbs/list` + `/v2/posting/fbo/list`. Колонки `scheme` (`fbs` — включает RFBS, `fbo`), `status` (delivered/delivering/cancelled/awaiting_*). payload: `products[]{sku, offer_id, name, price, quantity, currency_code}`, `financial_data{products,cluster_from,cluster_to}`, `cancellation`, `delivering_date`, `shipment_date`, `order_id/order_number`… **История только с 2026-05-01.** Сумма заказа = Σ price×quantity (цена продавца). |
| `raw_ozon_realization` | 1 строка = отчёт о реализации за месяц `(account, year, month)` | `year, month`, `loaded_at` | `/v2/finance/realization`: `header{…start_date, stop_date, doc_date}`, `rows[]{rowNumber, item{sku,offer_id,barcode,name}, seller_price_per_instance, commission_ratio, delivery_commission{quantity, amount, total, bonus, stars, bank_coinvestment, pick_up_point_coinvestment, compensation, standard_fee, commission, price_per_instance}, return_commission{…}}`. Σ `seller_price_per_instance × delivery_commission.quantity` = Σ `accruals_for_sale>0` транзакций (сверено до 0.15 %). Есть 2026-01…2026-08, сентябрь ещё не опубликован. Источник сплита «Продажи» (выручка/баллы/партнёры). |
| `raw_ozon_orders_report` | 1 строка = отправление `(account, posting_number)` | `delivered_at` (date, у части NULL), payload `processed_at, shipped_at`; `loaded_at` | Отчёт Ozon «по заказам» для ФТС (признак выкупа `is_buyout`), payload: `order_number, posting_amount, paid_by_customer, qty, sku, offer_id, status, buyout, items[]`. Заказывается run_daily только 1–5 числа за прошлый месяц (+ ручные дозаказы). Не ежедневный источник. С 2026-04-03. |
| `raw_ozon_attributes` | `(account, offer_id/sku)` снимок | `collected_at` | атрибуты карточек (payload), cron вс 04:15 |
| `raw_feedback` (общая) | `(platform, account, kind, ext_id)` | `created_at`, `collected_at` | отзывы/вопросы; Ozon: `kind in (review, question)` |
| `raw_mp_returns` / `mp_returns` / `mp_return_items` (общие) | `(platform, account, return_id)` | `created_at, arrived_at, received_at, first_seen, last_seen, gone_at` | возвраты FBS/ПВЗ (бот возвратов, поток ret): `amount, status_name, stage, scheme`; позиции — `mp_return_items{sku, offer_id, qty, price}` |

## 2. Реклама (Performance)

| Таблица | Гранулярность | Дата | Метрики |
|---|---|---|---|
| `ozon_ads` | месяц × кампания `(account, period, campaign_id)`; `period` = 1-е число месяца, `covered_to` = по какой день агрегат | `period`, `covered_to`, `updated_at` (обновляется не всегда — ориентир `covered_to`) | `spend, views, clicks, orders, sold, ad_revenue`, `adv_type, pay_model, state, title` |
| `ad_spend_daily` (общая) | день × аккаунт `(platform, account, date)` | `date`, `updated_at` | `spend` — весь расход Performance за день; Σ за месяц = Σ `ozon_ads.spend` |
| `mkt_ozon_ads_sku_daily` | день × кампания × SKU `(stat_date, account, campaign_id, sku)` | `stat_date`, `collected_at` | `bid, views, clicks, money_spent, orders_qty, orders_money, drr`. Только SKU-статистика CPC-кампаний: ≈35–45 % от `ad_spend_daily`. С 2026-07-27. |
| `ozon_bids` | снимок дня × кампания × SKU | `captured_at` | `bid, target_cir, state, adv_type`; дыра 2026-07-01…08-06 |
| `ozon_search_promo` | снимок дня × SKU (продвижение в поиске) | `captured_at`, `updated_at` | `bid, bid_without_additive, carrots_additive, views_week, views_prev_week, visibility_index, promo_status, available` |
| `mkt_ozon_bid_plan`, `mkt_ozon_bid_ramp`, `mkt_ozon_bid_step_log`, `mkt_ozon_bid_journal` | планы/журнал ставок (решения mkt) | `built_at / step_date / decided_on, week_start` | `bid_before/after/target/ceiling, margin_pct, cr, qty90, revenue90, m_before/m_after jsonb, outcome` — производные, не факт |

## 3. Цены, индекс цены, акции

| Таблица | Гранулярность | Дата | Метрики |
|---|---|---|---|
| `ozon_price_index` | снимок дня × SKU `(account, sku, collected_on)` | `collected_on`, `updated_at` | `price, old_price, marketing_price` (цена покупателя), `marketing_seller_price, min_price`, `color_index`, `external_min_price/external_index`, `ozon_min_price/ozon_index`, `self_min_price/self_index`, `commission_fbo_pct/commission_fbs_pct, acquiring, volume_weight, auto_action_enabled`. С 2026-08-06, пропуск 08-09. |
| `ozon_price_index_run` | прогон × аккаунт | `collected_on`, `finished_at` | `items, with_external, zones, api_calls` |
| `mkt_ozon_buyer_price` (+`_run`) | снимок × offer_id | `snapshot_date` (единственный: 2026-08-06) | `our_price, k, buyer_price, external_index, price_for_target, target_index` |
| `oz_action_seen`, `oz_action_log`, `oz_action_ladder` | акции Ozon: справочник / журнал добавлений / лестница цен | `first_seen, date_start/end / ts / last_step_on` | `action_price, floor/cap_price, rung, ok` |

## 4. Остатки

| Таблица | Гранулярность | Дата | Метрики |
|---|---|---|---|
| `ozon_fbo_stock` | снимок дня × SKU × склад | `captured_at` | `free_to_sell, reserved, promised` (FBO; малый объём — работаем по FBS) |
| `ozon_stock_signals` | снимок дня × SKU | `captured_at` | `days_without_sales, turnover_grade, excess_stock_count, ads` (среднесуточные продажи), `idc` (дней остатка) |
| `supplier_stock` (общая, без account) | снимок дня × товар МС × склад/поставщик | `captured_at` | `stock, reserve, in_transit, buy_price, cost_seb, stock_days, sold_30d`; ключ к Ozon `external_code = offer_id` |
| `ozon_removal_candidates`, `ozon_removal_submitted` | вывоз с FBO (вт) | `run_date / submitted_at` | `qty, days_without_sales, rules` |
| `stocks` (общая) | — | — | Ozon-строк нет (0) |

## 5. Аналитика / воронка / поиск

**Таблицы воронки Ozon (`hits_view`, `session_view`, конверсии из `/v1/analytics/data`) в БД НЕТ.** Общая `funnel` — 0 строк Ozon.
Заменители:

| Таблица | Гранулярность | Дата | Метрики |
|---|---|---|---|
| `ozon_search_product` | неделя × SKU | `period_start` (пн) … `period_end` (след. пн); `updated_at` | `position, unique_search_users, unique_view_users, view_conversion, order_count, gmv, category` |
| `ozon_search_query` | неделя × SKU × поисковая фраза | `period_start, period_end` | `position, unique_search_users, unique_view_users, view_conversion, order_count, gmv` |
| `ozon_search_run` | прогон × неделя | `period_start, period_end, finished_at` | `skus_total, skus_with_data, queries_rows, tail_dropped, api_calls` |
| `mkt_ozon_query_econ` | фраза × SKU, одна неделя 2026-07-27 | `period_start` | `demand, views, view_conv, orders, gmv, margin_own_live, bid_ceiling, verdict` — производная |
| `ozon_rating` | снимок SKU (только acc1) | `updated_at` (последний 2026-07-17) | `avg_rating, reviews_count, r1..r5` |

Ловушка: в `ozon_search_*` есть «хвостовые» неполные периоды с тем же `period_start`, что и полная неделя
(`2026-07-27..07-29` рядом с `2026-07-27..08-03`) и однодневный `2026-09-07..09-08`. Группировать по паре
`(period_start, period_end)` и брать только `period_end - period_start = 7`.

## 6. Себестоимость и маржа

| Таблица | Гранулярность | Дата | Метрики |
|---|---|---|---|
| `margin_by_sku` (общая; пишет ТОЛЬКО fin) | месяц × SKU `(article=Ozon sku, platform, account, period_from, period_to)` | `period_from/to` = границы месяца; `computed_at` — **время ПЕРВОЙ вставки строки** (upsert его не обновляет → не показатель пересборки) | `revenue_buyer` (= Σ accruals_for_sale>0 по operation_date), `cogs` (FIFO отгрузок МС − сторно возвратов), `commission, logistics, returns_sum, storage, other` (знак +расход), `net_profit, margin_pct, commission_pct`; `qty` = NULL для Ozon. Операции без items[] (реклама-кампании, подписка, эквайринг) в строки SKU НЕ входят — «Оверхед» только в логе. Строится `reports/margin_ozon_sku.py`, в run_daily для текущего и прошлого месяца, гейт — успех транзакций/постингов/каталога/себеста. |
| `ms_demand_cogs` / `ms_demand_pos` | отгрузка МС (агенты «Покупатель Озон», «Озон Экспресс»; `org` = юрлицо ↔ аккаунт: `a5e7ee84…`=acc1, `f5bea6e0…`=acc2) | `moment`, `loaded_at` | `cogs, qty, npos`; позиции `cost, qty` |
| `ms_return_cogs` | возврат МС | `moment, ym`, `loaded_at` | `sellable, ret_qty, unit_cogs, storno_cogs` |
| `oz_cogs_demand`, `oz_cogs_manual`, `oz_cogs_frozen` | себест по документам отгрузки для «Отчёты МП» / ручное закрытие месяца | `demand_date, ym`, `loaded_at`; frozen: закрыты 2025-11…2026-07 | `our_sum, qty, cogs, method, status` |
| `mkt_ozon_margin_control` | снимок × offer_id (единственный 2026-08-07) | `captured_date, built_at` | `our_price, buyer_price, payout_ratio, to_pay_u, logistics_u, other_rate, cogs_u, margin_own_live/fifo, verdict…` |
| `cogs_actual`, `cogs_manual` (общие) | месяц × площадка / ручной себест | `month / —` | `cogs`, `unit_cost` |

## 7. Каталог и карточки

| Таблица | Ключ | Дата | Содержимое |
|---|---|---|---|
| `ozon_product` | `(account, sku)` | `updated_at` (при изменении) | `offer_id, name, is_archived` |
| `ozon_dims` | `(account, offer_id/sku)` | `updated_at` | габариты Ozon `depth/width/height_mm, weight_g, volume_l` |
| `card_status` (общая) | `(platform, account, offer_id)` | `first_seen, last_seen, healed_at` | статусы/ошибки карточек |
| `sku_relations` (общая) | связи SKU | `built_at` | `rel_type, item_from/to` |
| `biz_events`, `mp_notices` (общие) | события / новости площадок | `event_date / created_at` | контекст для объяснения просадок |

## 8. Где брать типовые метрики (быстрый указатель)

- **Заказы шт/₽ по SKU×день**: `raw_ozon_posting` → `jsonb_array_elements(payload->'products')` (`sku, offer_id, price, quantity`), день по `in_process_at` MSK, `status<>'cancelled'`. С 2026-05-01.
- **Отмены**: `raw_ozon_posting.status='cancelled'` (+ `payload->'cancellation'`, `cancel_reason_id`); возвраты — `accruals_for_sale<0` в транзакциях, физические — `mp_returns`/`mp_return_items`.
- **Выручка, комиссия, логистика, услуги по SKU×день**: `raw_ozon_transaction` с `items[]` (sku) — `accruals_for_sale`, `sale_commission`, `delivery_charge/return_delivery_charge`, `services[]`; раскладка `collectors/ozon.py::categorize_operation`. Месячно по SKU — `margin_by_sku` (без оверхеда).
- **Реклама кампания×SKU×день**: `mkt_ozon_ads_sku_daily` (с 27.07); по аккаунту×день — `ad_spend_daily`; кампания×месяц — `ozon_ads`.
- **Показы/трафик по SKU**: воронки по дням нет; недельно — `ozon_search_product.unique_view_users/unique_search_users/position`; рекламные показы по дням — `mkt_ozon_ads_sku_daily.views`; `ozon_search_promo.views_week` (снимок недели).
- **Остатки SKU×день**: `ozon_fbo_stock` (FBO), `ozon_stock_signals.idc/ads`, МС — `supplier_stock` по `external_code=offer_id`.
- **Цена/индекс SKU×день**: `ozon_price_index` (с 06.08).

## Общие таблицы без Ozon-данных

`sales`, `funnel`, `ads`, `stocks`, `raw_positions`, `drops` — строк с platform/account Ozon нет (WB-слой).
`fts_posting_status` — 14 строк Ozon (служебная).

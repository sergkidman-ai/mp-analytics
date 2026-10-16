# Ozon Seller API и Performance API: аудит устаревших методов, 22.09.2026

Только анализ: код не менялся, ничего не коммитилось. Живые запросы шли только к методам чтения
под `oz_acc1` с минимальными телами (limit 1, 1–7 дней). Мутирующие методы и создание отчётов не вызывались.
Область поиска: `/opt/mp-analytics`, кроме venv, `.claude/worktrees`, pgdata, backups и `.deploy/inv`
(это копия collectors, те же пути). Файлы scratch_* вынесены в отдельный список в конце.

## Итог

- В коде **52 уникальных эндпоинта Ozon**: 40 Seller, 12 Performance. Живьём проверено **39 методов чтения**: 32 Seller, 7 Performance и `transaction/totals`, которого в коде нет.
- **Отключены (400 `{"code":9,"message":"obsolete method cannot be used"}`): 2.**
  - `/v3/finance/transaction/list` используется в `collectors/ozon.py:34` (`fetch_transactions`).
  - `/v3/finance/transaction/totals` в коде не встречается.
- **Закрыт не как устаревший:** `/v1/review/list` и `/v1/review/comment/list` отдают 403 code 7, потому что
  у юрлица нет подписки Premium Plus (об этом уже написано в `collectors/ozon_feedbacks.py:57`). Эти методы
  вызывает только `run_marketing.py` (ручной запуск, в cron его нет) через `collectors/ozon_reviews.py`
  и `collectors/ozon_review_answers.py`.
- Остальные 35 проверенных методов чтения живы. Ответы 404, 405 и 400 code 3 в таблице получены на
  фиктивные id или неверное тело пробы. Повтор с правильным телом дал 200.
- **Не проверялись, потому что мутируют или создают отчёт: 12.** Список в разделе 3.

## 1. Методы чтения: живая проверка (oz_acc1, 22.09.2026)

| Эндпоинт | Результат | Где в коде | Кто вызывает |
|---|---|---|---|
| POST /v3/finance/transaction/list | **400 code 9 obsolete** | collectors/ozon.py:34,166 | run_daily, шаг «Ozon транзакции {acc} {месяц}» (cron 2:07 и 19:07) |
| POST /v3/finance/transaction/totals | **400 code 9 obsolete** | не используется | — |
| POST /v3/product/list | 200 | collectors/ozon.py:182; ozon_products.py; ozon_card_status.py:23; ozon_search_queries.py:132; ops/ozon_stock_action.py:151; launch_track.py:127; ops/ozon_promo_bonus_watch.py; tools/ozon_card_partial_push_test.py | run_daily «Ozon каталог», шаг «в продаже» (run_daily.py:237), cron: card_status 7:41, search_queries ср 3:23, stock_action 8:47/9:05, promo_bonus_watch, launch_track 5:23 |
| POST /v3/product/info/list | 200 | collectors/ozon.py:183; ozon_products.py; ozon_card_status.py:128; ops/ozon_stock_action.py:141,159; tools/ozon_card_push.py; tools/ozon_card_c_report.py | как в строке выше, плюс cron ozon_card_push 8:14 |
| POST /v4/product/info/attributes | 200 | ozon_attributes.py:26; ozon_card_status.py:47; ozon_dims.py:20; reports/feedback_drafts.py; tools/ozon_card_push.py:59 | cron: attributes вс 4:15, card_status, card_push; feedback-cycle.service (черновики) |
| POST /v5/product/info/prices | 200 | collectors/ozon_price_index.py:48 | cron 4:11 |
| POST /v2/analytics/stock_on_warehouses | 200 | collectors/ozon_fbo_stock.py:21; ops/ozon_stock_action.py:171 | run_daily «Ozon ФБО остатки»; cron stock_action |
| POST /v1/analytics/stocks | 200 (в логе 8× 500 за 2 недели, сбой случайный) | collectors/ozon_stock_signals.py:26 | run_daily «Ozon сигналы оборачиваемости» |
| POST /v1/analytics/product-queries | 200 | collectors/ozon_search_queries.py:119,198 | cron ср 3:23 |
| POST /v1/analytics/product-queries/details | 200 (при первой пробе 429 из-за частоты) | collectors/ozon_search_queries.py:223 | cron ср 3:23 |
| POST /v2/finance/realization | 200 | collectors/ozon_realization.py:30,50 (web/app.py:4284 только комментарий) | run_daily «Ozon отчёт о реализации» |
| POST /v2/posting/fbo/list | 200 | collectors/ozon_postings.py:25; ozon_orders_report.py | run_daily «Ozon постинги», «ФТС: отчёт Ozon по заказам» |
| POST /v3/posting/fbs/list | 200 | collectors/ozon_postings.py:24; ozon_orders_report.py | то же |
| POST /v3/posting/fbs/get | 200 | упомянут в докстринге ozon_orders_report.py:4 | — |
| POST /v1/report/info | 404 «report … not found» на фиктивный code (метод жив) | collectors/ozon_orders_report.py:39 | run_daily ФТС |
| GET /v1/actions | 200 | ops/ozon_stock_action.py:107 | cron stock_action; logs/oz_action_probe.py (cron помечен «ВРЕМЕННО до 24.08», до сих пор работает) |
| POST /v1/actions/candidates | 200 (с реальным action_id) | ops/ozon_stock_action.py:114 | то же |
| POST /v1/actions/products | 200 (с реальным action_id) | ops/ozon_stock_action.py:127 | то же |
| POST /v1/product/rating-by-sku | 200 | ops/ozon_promo_bonus_watch.py:38 | cron вт 7:23, чт 6:23 |
| POST /v1/question/list | 200 | collectors/ozon_feedbacks.py:21 | feedback-collect.service (feedback_collect_all) |
| POST /v1/question/answer/list | 200 (с реальным question_id) | collectors/ozon_question_answers.py:22 | сбор ответов на вопросы |
| POST /v1/review/list | **403 code 7** (нет подписки) | collectors/ozon_reviews.py:20; ozon_review_answers.py | run_marketing.py (вручную) |
| POST /v1/review/comment/list | **403 code 7** (нет подписки) | collectors/ozon_review_answers.py:21 | вручную |
| POST /v1/removal/from-stock/list | 200 (даты в формате YYYY-MM-DD, как в коде) | returns_bot/sources/ozon_removal.py:40 | returns-bot / returns-daily / returns-weekly (.service) |
| POST /v1/removal/from-supply/list | 200 | returns_bot/sources/ozon_removal.py:41 | то же |
| POST /v1/returns/list | 200 | returns_bot/sources/ozon.py:42; ozon_rfbs.py; returns_bot/collect.py | returns_bot.* |
| POST /v2/returns/rfbs/list | 200 | returns_bot/sources/ozon_rfbs.py:41; returns_bot/pending.py:35 | returns_bot.* |
| POST /v2/returns/rfbs/get | 200 (поле `return_method_description` Ozon пометил устаревшим) | returns_bot/sources/ozon_rfbs.py:49 | returns_bot.* |
| POST /v1/return/giveout/barcode | 200 (GET даёт 405) | returns_bot/sources/ozon.py:56 | returns_bot |
| /v1/return/giveout/get-png, get-pdf | не проверялись: выдают файл, метод чтения той же группы жив | returns_bot/sources/ozon.py:58,68; returns_bot/codes.py:9 | returns_bot |
| POST /v3/chat/list | 200 (в логе 2× 400 за 2 недели, oz_acc1) | ops/mp_news.py:162 | cron 9:17 и 19:17 |
| POST /v3/chat/history | 404 «chat not found» на фиктивный id (метод жив) | ops/mp_news.py:179 | то же |
| POST /v1/product/import/info | 404 «task not found» на фиктивный id (метод жив) | tools/ozon_card_push.py:61; scratch_ozon_apply_dims.py:10 | cron ozon_card_push 8:14 |
| Perf POST /api/client/token | 200 | collectors/ozon_ads.py:59; web/app.py; tools/* | все сборщики Performance |
| Perf GET /api/client/campaign | 200 | ozon_ads.py:120; ozon_bids.py:86; tools/ozon_cannibal.py:37; tools/ozon_new_campaigns.py:33 | cron 5:47 (ads+bids), ads 3-го числа 5:23 |
| Perf GET /api/client/campaign/{id}/v2/products | 200 | ozon_bids.py:53; tools/ozon_cannibal.py:43; ozon_e7b_restore.py:36; ozon_wave1_delete.py:38 | cron 5:47 |
| Perf POST /api/client/campaign/search_promo/v2/products | 200 | ozon_bids.py:69 | cron 5:47 |
| Perf GET /api/client/statistics/daily/json | 200 | collectors/ozon_ads.py:85 | cron 5:47 |
| Perf GET /api/client/statistics/{uuid} | 404 «report not found» на фиктивный UUID (метод жив) | tools/ozon_bid_ramp.py:68 | вручную |
| Perf GET /api/client/statistics/report | 404 «report not found» на фиктивный UUID (метод жив) | tools/ozon_bid_ramp.py:72 | вручную |

Сырые ответы: `/tmp/claude-0/-root/e0836217-f273-44c7-a041-ac7442556f45/scratchpad/probe*.json`.

## 2. Замена для отключённого /v3/finance/transaction/list

**Последствия.** С прогона run_daily **08.09.2026 19:07** шаг «Ozon транзакции» падает на обоих аккаунтах:
в логе 112 `[FAIL]`. Последний успешный прогон был 08.09 02:07. Цепочка последствий:
- `raw_ozon_transaction` заморожена на 08.09;
- run_daily пропускает «Ozon маржа» (112 × `[skip] … упал источник: Ozon транзакции`), и витрина маржи Ozon не обновляется;
- устаревают данные, которые читают таблицу: web/app.py (живой Ozon-месяц и прогноз, вкладка «Отчёты МП · Ozon»),
  reports/ozon_mp_report.py и ozon_mp_freeze (cron 1-го числа: 01.10 закроет сентябрь по неполным данным),
  ozon_expenses, ozon_margin_control, mp_cogs_drill, collectors/ms_demand_cogs.py (сторно COGS),
  ops/ozon_promo_bonus_watch.py, ops/ozon_lines_audit.py, tools/ozon_eval_core, ozon_halo_eval, ozon_acc1_plan*.

**Замена по объявлению Ozon (dev.ozon.ru, канал @OzonSellerAPI):**
- `POST /v1/finance/accrual/by-day` принимает `{"date":"YYYY-MM-DD"}`: **один день за запрос**, страницы по `last_id`.
  Живая проверка дала 200: `accruals[]` с полями accrual_id, date, total_amount, unit_number,
  accrued_category (POSTING/ITEM/NON_ITEM), posting, item_fees, non_item_fee, container_fees.
- `POST /v1/finance/accrual/postings` принимает `{"posting_numbers":[1..200]}` и возвращает начисления по отправлениям (200).
- `POST /v1/finance/accrual/types` отдаёт справочник type_id → name (200, около 120 типов).

**Объём правки: средний, около 1–2 дней с учётом сверки.** Уже есть готовая база:
`docs/reference/ozon_accrual_to_txn_mapping.md` и `.json` (22.09). Там на 6986 парах показано, что адаптер
«начисление → payload старого формата» совпадает на 100 % по суммам и сходится с балансом ozon_mp_report
до копейки. Поэтому правильный путь такой: переписать `fetch_transactions` в `collectors/ozon.py`
(цикл по дням через by-day плюс адаптер) и оставить схему `raw_ozon_transaction` без изменений.
Тогда не придётся трогать ни одного из примерно 15 потребителей. Что остаётся открытым (подробности в том же документе):
- posting.order_date не восстанавливается, нужен суррогат для margin_ozon_sku;
- operation_type у type 71 и 25;
- `ozon_lines_registry` надо дополнить новыми type_id;
- сбор постингов должен идти раньше адаптера;
- после выкатки дозалить 08.09 → сегодня за 08 и 09 месяцы.

Таблица `raw_ozon_accrual` в БД уже есть, но в репозитории её никто не пишет: её заполнял прототип.

## 3. Мутирующие и создающие отчёт: НЕ проверялись

| Эндпоинт | Файлы | Кто вызывает |
|---|---|---|
| POST /v1/actions/products/activate | ops/ozon_promo_guard.py:177; ops/ozon_stock_action.py:495 | cron promo_guard 3:30/17:30/23:30/10:30; stock_action apply 9:05 |
| POST /v1/actions/products/deactivate | ops/ozon_promo_guard.py:190; ops/ozon_stock_action.py:510 | то же |
| POST /v1/product/attributes/update | tools/ozon_card_push.py:60 | cron 8:14 `--apply` |
| POST /v3/product/import | scratch_ozon_apply_dims.py:9 (+ tools/hooks/guard_side_effects.py, это гард) | вручную |
| POST /v1/question/answer/create | collectors/feedback_send.py:43 | feedback-cycle.service |
| POST /v1/review/comment/create | collectors/feedback_send.py:44; feedback_autosend.py:75; reports/feedback_today.py:70 | feedback-cycle (при подписке 403) |
| POST /v1/report/postings/create (создаёт отчёт) | collectors/ozon_orders_report.py:38 | run_daily «ФТС: отчёт Ozon по заказам» (периодически) |
| Perf POST /api/client/statistics/json (создаёт отчёт) | tools/ozon_bid_ramp.py:62 | вручную |
| Perf POST/PUT /api/client/campaign/{id}/products (ставки/добавление) | tools/ozon_bid_ramp.py:129; ozon_core_push.py:202; ozon_e7b_restore.py:114; ozon_new_campaigns.py:50; **web/app.py:4507** (ставка из дашборда) | вручную / mp-dashboard.service |
| Perf POST /api/client/campaign/{id}/products/delete | tools/ozon_wave1_delete.py:59; tools/ozon_acc2_cleanup.py:52 | вручную |
| Perf POST /api/client/campaign/cpc/v2/product (создание кампании) | tools/ozon_new_campaigns.py:44 | вручную |
| Perf /api/client/statistic/orders/generate/json | только докстринг collectors/ozon_ads.py:24 | — |

По каналу Ozon уже **отключены** методы `/v2/returns/rfbs/{reject,compensate,verify,receive-return,return-money}`
(замена: `/v1/returns/rfbs/action/set`) и `/v2/posting/fbs/act/create`. В коде их нет, проверено grep.

## 4. Логи за 2 недели (08.09–22.09)

| Лог | Что падает | Счёт |
|---|---|---|
| run_daily.log | `[FAIL] Ozon транзакции` → 400 на /v3/finance/transaction/list (obsolete) | 112 |
| run_daily.log | `[skip] Ozon маржа … упал источник: Ozon транзакции` | 112 |
| run_daily.log | `[FAIL] Ozon сигналы оборачиваемости` → 500 на /v1/analytics/stocks (сбой на стороне Ozon, не устаревание) | 8 |
| logs/mp_news.log | oz_acc1 `400 Bad Request` на /v3/chat/list (сейчас 200) | 2 |
| logs/oz_stock_action.log | ошибка на /v3/product/info/list (сейчас 200) | 1 |

Строк `obsolete`, `"code":9` или `deprecated` в логах нет: requests пишет в лог только статус 400 без тела.
Ошибки Performance `statistics/report 404` («Ozon реклама») встречаются только в старой части run_daily.log, за 2 недели их нет.

## 5. scratch_* (низкий приоритет)

- `scratch_ozon_apply_dims.py:9–10`: /v3/product/import (мутирующий), /v1/product/import/info (жив).
- `scratchpad/p1/finalize_sets.py:17`: Perf /api/client/campaign (жив).
- В `docs/reports/ozon_decline_2026-09-14/queries/*` читают `raw_ozon_transaction` из БД, API не вызывают.

## Попутно

- В cron осталась строка `logs/oz_action_probe.py` с пометкой «ВРЕМЕННО до 24.08». Она каждый час с 18 до 8 дёргает /v1/actions*.

# BRIEF_FIN — поток «Финансы / экономика»

> Первым делом прочитай этот бриф + `CLAUDE.md` + `ARCHITECTURE.md`. Ты — сессия домена **fin**.
> Параллельно может идти сессия домена **mkt** (Джем/реклама/воронка/ABC) — не лезь на её территорию.

## Зона ответственности
Себестоимость, COGS, маржа по SKU, выручка/расходы, P&L по площадкам, opex, сверка с эталоном,
модель периода, поставщики/закупки/остатки МС, оборачиваемость, ABC-маржа (часть «маржа»).

## Твои файлы (правишь ты)
- Коллекторы: `collectors/{moysklad, ms_products, ms_demand_cogs, wb, ozon, ozon_postings,
  ozon_products, ozon_fbo_stock, yandex, yandex_monthly, suppliers, supplier_purchases, set_cost}.py`
- Витрины: `reports/{margin_by_sku, margin_ozon_sku, ozon_expenses}.py`
- Оркестратор: `run_daily.py`
- Скрипты: `rebuild_validate_cogs.py`, `phase1_cogs.py`, `cogs_compare.py`, promo/анализ экономики
- Миграции: **блок 0xx** (следующая — `028_*`). НЕ бери 1xx (это mkt).

## Твои таблицы (пишешь)
`products, sales, margin_by_sku, ms_demand_cogs, cogs_actual, opex, stocks, supplier_*,
ms_product, ms_barcode, set_cost, yandex_cost, yandex_monthly, raw_*` (финансовые).

## Граница (важно)
- **`margin_by_sku` — ТВОЯ собственность, ты её ПИШЕШЬ.** Маркетинг её только читает.
- Источник себеста — ТОЛЬКО `report/stock/byoperation` (память `project_mp_analytics_cogs`). Не возвращайся
  к cost_seb/byPrice/усреднению.

## СТОП-зоны (территория mkt — не трогай)
Джем (`wb_jam`, `wb_search_*`), реклама (`wb_ads`, `ozon_ads`, `ozon_bids`, `ad_spend_daily`),
воронка (`wb_funnel`), отзывы/рейтинг (`ozon_reviews`, `ozon_rating`), `drops`, `run_marketing.py`.
Если задача тянет тебя туда по контексту или коду — **СТОП, сообщи пользователю** (это сигнал, что
границу пора пересматривать).

## Параллельная работа (worktree-изоляция)
Так как сессии идут одновременно — работай в СВОЁМ git worktree и доменной ветке:
```bash
git worktree add .claude/worktrees/fin -b fin/<задача> origin/main   # или из текущего main
cd .claude/worktrees/fin && echo fin > .workstream
```
Имя ветки `fin/*` включает флаг территории автоматически.

## Флаг чужой территории
Активен git-хук (`tools/hooks/pre-commit` → `tools/territory_guard.py`). При коммите правок ЧУЖИХ
файлов из ветки `fin/*` — коммит блокируется с пояснением. Самопроверка в любой момент:
```bash
python3 tools/territory_guard.py --status
```
Осознанный обход: `WORKSTREAM_OVERRIDE=1 git commit ...`.

## Дисциплина
Чекпоинты в `docs/` и память (`project_mp_*` твои); локальные коммиты; **push только с ОК**;
секреты не печатать; следить за размером сессии.

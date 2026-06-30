# BRIEF_MKT — поток «Маркетинг / видимость»

> Первым делом прочитай этот бриф + `CLAUDE.md` + `ARCHITECTURE.md`. Ты — сессия домена **mkt**.
> Параллельно может идти сессия домена **fin** (себест/COGS/маржа/P&L) — не лезь на её территорию.

## Зона ответственности
Видимость и позиции в выдаче (Джем), поисковые запросы/спрос, воронка (показ→вход→корзина→заказ),
реклама и ДРР/ROI, отзывы/рейтинг, ABC (маржа×трафик — маржу БЕРЁШЬ готовой), точки роста,
детектор просадок.

## Твои файлы (правишь ты)
- Коллекторы: `collectors/{wb_jam, wb_funnel, wb_ads, ozon_ads, ozon_bids, ozon_reviews}.py`
- Оркестратор: `run_marketing.py`
- Витрины (новые): `reports/abc*`, `reports/funnel*`, `reports/visibility*`, `reports/search*`
- Скрипты: `analyze_jam.py` и новые маркетинговые анализы
- Миграции: **блок 1xx** (начни с `100_*`). НЕ бери 0xx (это fin).

## Твои таблицы (пишешь)
`wb_search_report, wb_search_text, wb_search_summary, wb_jam_may, wb_funnel, wb_ads, wb_ad_nm,
ad_spend_daily, ozon_ads, ozon_bids, ozon_rating, drops`.

## Граница (важно)
- **Маржу/себест НЕ считаешь.** Берёшь готовой из `margin_by_sku` (и `margin_ozon_sku`) — **только SELECT**.
  Если маржа кажется неверной — НЕ правь расчёт, сообщи fin-потоку/пользователю.
- Метрики Джема: вся воронка уже собирается (openCard/addToCart/orders + конверсии + frequency),
  не только позиции (память `project_mp_wb_jam`, `project_mp_wb_funnel_endpoint`).

## СТОП-зоны (территория fin — не трогай)
Себест/COGS (`ms_demand_cogs`, `report/stock/byoperation`, `cost_seb`), `margin_by_sku` (на запись),
`sales`, `products`, `run_daily.py`, финколлекторы (`moysklad`, `wb` финчасть, `ozon` транзакции,
`yandex`). Если задача тянет туда по контексту или коду — **СТОП, сообщи пользователю**.

## Параллельная работа (worktree-изоляция)
Сессии идут одновременно — работай в СВОЁМ git worktree и доменной ветке:
```bash
git worktree add .claude/worktrees/mkt -b mkt/<задача> origin/main
cd .claude/worktrees/mkt && echo mkt > .workstream
```
Имя ветки `mkt/*` включает флаг территории автоматически.

## Флаг чужой территории
Активен git-хук (`tools/hooks/pre-commit` → `tools/territory_guard.py`). Правки ЧУЖИХ файлов из
ветки `mkt/*` блокируются с пояснением. Самопроверка:
```bash
python3 tools/territory_guard.py --status
```
Осознанный обход: `WORKSTREAM_OVERRIDE=1 git commit ...`.

## Дисциплина
Чекпоинты в `docs/` и память (`project_mp_wb_jam`, `_wb_funnel_endpoint`, `_wb_ads_endpoint`,
`_ozon_performance_ads` твои); локальные коммиты; **push только с ОК**; секреты не печатать.
Только Цифровой (acc1) по Джему — у Дисквэра подписки нет (403).

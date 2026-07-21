# SESSIONS_MAP — карта рабочих потоков и tmux-сессий

> Единый реестр: **один тематический поток = одна живая tmux-сессия с каноничным именем**.
> Сессии копятся (mkt→mkt2→mkt3→mkt4, reviews/review3…), имена версионируются вручную, старые дубли
> висят с прогоревшим контекстом — эта карта против этого. Обновлять при заведении/сворачивании сессии.
> Правила территорий — в `CLAUDE.md` (правило «Потоки»); состояние каждого потока — в `docs/HANDOFF.md`.
> Обновлено 2026-07-21.

## Вход в сессию (шпаргалка)
```
ssh <сервер>            # зайти на машину
tmux ls                 # список сессий (см. каноничные имена ниже)
tmux attach -t <имя>    # подключиться (напр. tmux attach -t mkt)
# уже внутри tmux:  Ctrl+b d  — отсоединиться (сессия живёт дальше)
#                   Ctrl+b s  или  tmux switch-client -t <имя>  — перейти в другую сессию
```

## Потоки mp-analytics

| Поток | Каноничная tmux | Папка / worktree | Ключевые файлы | Статус | Где handoff |
|---|---|---|---|---|---|
| **fin** — финансы/счета, БИ, сверки, COGS/маржа, P&L | `fin` | `.claude/worktrees/fin-night` (ветка `fin/*`) + дашборд :8090 | `run_daily.py`, `reports/margin_by_sku.py`, `margin_ozon_sku.py`, `ozon_expenses.py`, `web/app.py`, `collectors/{moysklad,wb,ozon,yandex}*`, `migrations/0xx` | ✅ живая (`fin`, заведена 2026-07-21) | `docs/HANDOFF.md#финансы` + `BRIEF_FIN.md`, `FIN_DATA_FOR_MKT.md` |
| **mkt** — маркетинг: реклама/органика, Джем, воронка, ДРР, ABC, ставки | `mkt` | `.claude/worktrees/mkt` (ветка `mkt/start`) + сервис :8092 | `run_marketing.py`, `reports/sku_economics.py`, `web/marketing_app.py`, `collectors/{wb_jam,wb_prices,wb_market_price,*_ads,*_funnel}`, `migrations/1xx` | ✅ живая (`mkt4`) | `docs/HANDOFF.md#маркетинг` + `BRIEF_MKT.md`, `MKT_WB_UNIT_ECONOMICS.md`, `HANDOFF_MKT.md` |
| **rev** — отзывы/вопросы/чаты, ответчик, ТГ-модерация | `rev` | `/opt/mp-analytics` (ветка `eng/deepseek-answer-engine`) | `reports/feedback_{llm,drafts,grounding,corpus,send,today,web,sample}.py`, `collectors/{wb,ozon}_feedbacks.py`, `raw_feedback` (мигр. 038), ТГ-бот модерации | ✅ живая (`review3`) | `docs/HANDOFF.md#отзывы` + `docs/review3_handoff.md` |
| **gab** — габариты карточек, переплата логистики | `gab` | `/opt/mp-analytics` | `supplier_dims` (мигр. 036/037), `scratch_dims_*.py`, `docs/*dims*`, `docs/wb_logistics_overpay.*`, коллекторы поставщиков (RAPID/ГалаПринт/Солюшнс) | ✅ живая (`gabarity`) | `docs/HANDOFF.md#габариты` + `docs/GABARITY_CONTEXT.md` |
| **inv** — приёмка УПД→заказ поставщику, чистка МС | `inv` | `/opt/mp-analytics/invoice_bot` (сессия из `/root`) | `invoice_bot/{invoice_to_po,mail_poller,tg_bot,upd_to_supply,ms,supplier_groups,workcal,proc_log}.py` | ✅ живая (`invoice`) | `docs/HANDOFF.md#invoice` |

**Аналитика** — не отдельный поток, а режим внутри `fin`/`mkt` (`analyze_*.py`, `*_plan.py`, разовые витрины).
Разовый анализ ведёт профильный поток; отдельную сессию не заводить.

## Внешние проекты (НЕ mp-analytics — правила этого репо на них не распространяются)

| Проект | Каноничная tmux | Папка | Статус |
|---|---|---|---|
| **china-audit** | `china` | `/opt/china-audit` | ✅ живая (`china`), свой git/память |
| **sokol** (анализ ТЗ) | `sokol` | `/opt/sokol-server`, `/opt/tz-analyzer-src`, `/var/www/sokol` | нет живой сессии; заводить только под задачу Сокола |

## Схема имён
Каноничные имена — короткие, по потоку: **`fin` · `mkt` · `rev` · `gab` · `inv`** (mp-analytics),
**`china` · `sokol`** (внешние). Одно имя на поток, **без версионных суффиксов** (`mkt`, а не `mkt3/mkt4`).

## Правила жизненного цикла
1. **Одно каноничное имя на поток**, ровно из таблиц выше.
2. **Версионные суффиксы `-N` запрещены на живых сессиях.** Переполнился контекст → перенос состояния в
   `docs/HANDOFF.md`, `/clear` (или новый процесс под тем же именем), предшественника **убить**.
3. **Предшественника убивают, не переименовывают** (`tmux kill-session -t <old>`) — иначе висит прогоревший дубль.
4. **Один поток — свой worktree/ветка.** Несколько сессий на общем `/opt/mp-analytics` (сейчас `rev`/`gab`) —
   гонка правок; со временем каждому потоку свой worktree, как у `mkt`/`fin`.
5. **Голую `claude`-сессию не заводить** — имя называет поток. Сгруппированные сессии, показывающие один
   процесс под вторым именем, — убрать лишнее имя.
6. **Ночные задания — в профильной сессии** (`fin`/`mkt`/`rev` под `IS_SANDBOX=1`, см. память «ночной режим»),
   отдельная постоянная «ночная» сессия не нужна.

## Уборка выполнена 2026-07-21 ✅ — живые сессии: `china fin gab inv mkt rev`
Снесены дубли `claude` (алиас mkt4), `mkt3`, `mp-mkt2`, `reviews` (мёртвый bash), `mp-night` (домен отзывов,
влит в `rev`). Переименования: `mkt4→mkt`, `review3→rev`, `gabarity→gab`, `invoice→inv`. Заведена выделенная
**`fin`** (поток был сиротой: worktree `fin-night` есть, сессии не было — теперь есть). Осталось: `inv` без
своего handoff-дока (секция `#invoice` заведена в HANDOFF, вести там).

Историческая инвентаризация (снимок ДО уборки, для справки):

| tmux-имя | поток | было | вердикт | итог |
|---|---|---|---|---|
| `mkt4` | mkt | живая, текущая | ОСТАВИТЬ | → `mkt` ✅ |
| `claude` | mkt | дубль `mkt4` (тот же pane_pid 1348531) | УБИТЬ | снесён ✅ |
| `mkt3` | mkt | дубль-предшественник | УБИТЬ | снесён ✅ |
| `mp-mkt2` | mkt | дубль-предшественник | УБИТЬ | снесён ✅ |
| `review3` | rev | живая (ТГ-модерация) | ОСТАВИТЬ | → `rev` ✅ |
| `reviews` | — | мёртвый bash-шелл | УБИТЬ | снесён ✅ |
| `mp-night` | rev | пересекалась с review3 | СВЕРНУТЬ | влита в `rev` ✅ |
| `gabarity` | gab | живая | ОСТАВИТЬ | → `gab` ✅ |
| `invoice` | inv | живая | ОСТАВИТЬ | → `inv` ✅ |
| `china` | внешний | живая | ОСТАВИТЬ | → `china` ✅ |

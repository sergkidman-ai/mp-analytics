# Финмодель и данные для юнит-экономики (шпаргалка, общая для fin/mkt)

> Назначение: единый справочник — **где брать себестоимость, как считаются расходы по всем
> площадкам, и как маркетинговая ветка читает готовую финансовую юнит-экономику**.
> Всё живёт в одной БД `mp_analytics` (Postgres `127.0.0.1:5433`); дублировать расчёты не нужно —
> читать готовую витрину `margin_by_sku`. Сверено с БД 2026-07-20.

---

## 0. TL;DR для маркетинга

Юнит-экономика **уже посчитана** и лежит в витрине **`margin_by_sku`** (на SKU × месяц × площадку).
Ничего доскачивать/досчитывать не нужно — только читать. Ключ:
- **WB:** `margin_by_sku.article` = `nm_id` (числовой). Совпадает с ключом Джема/позиций.
- **Ozon:** `margin_by_sku.article` = Ozon `sku`.
- Свежая замещающая себестоимость (для форвардных расчётов акций/закупок) — `ms_product.buy_price` по `article`.

Граница доменов: **`margin_by_sku` пишет только `fin`; `mkt` читает её read-only.** Не пересобирать.

---

## 1. Доступ из worktree `mkt` (частый затык)

В отдельном git-worktree **нет своего `venv`**, а `.env` — симлинк на корневой. Поэтому:

```bash
# из /opt/mp-analytics/.claude/worktrees/mkt
/opt/mp-analytics/venv/bin/python your_script.py
```

```python
import os, sys
from dotenv import load_dotenv
load_dotenv(os.path.join(os.getcwd(), ".env"))   # симлинк → /opt/mp-analytics/.env (DATABASE_URL)
sys.path.insert(0, "/opt/mp-analytics")           # core.db из основного дерева
from core import db
rows = db.query("SELECT ... FROM margin_by_sku WHERE platform='wb' AND account='wb_acc1'")
```

БД одна на все ветки — сырьё и витрины, скачанные fin-ветками, видны mkt сразу.

---

## 2. Себестоимость (COGS) — единый источник

**🔒 Железное правило (клиент): себест ПРОДАННОГО = отгрузка МойСклад. Ничего кроме.**

| Что нужно | Откуда брать | Как |
|---|---|---|
| Себест/шт для анализа | витрина **`margin_by_sku`** (`cogs` / `qty`) | готовое, покрытие ~100%; ключ `article`, фильтр `platform` |
| Себест конкретной отгрузки (факт) | `report/stock/byoperation?operation.id=<demand_id>` | FIFO на `moment` документа; кэш → `ms_demand_cogs` (миграция 027) |
| Замещающая (акции/закупки/дефицит) | `ms_product.buy_price` | свежая закупочная, обновляется ежедневно из прайсов поставщиков |

**НЕ использовать** (дырявые пути, обжигались многократно):
- `buyPrice`/`cost_seb` карточки для проданного;
- усреднённый `report/profit/byproduct` на конкретной отгрузке;
- наивный джойн `external_code → cost_seb` (external_code не уникален → ложные «дыры»).

**Связка себеста по площадкам** (уже реализована в витринах):
- **WB:** `assembly_id` = имя отгрузки МС напрямую (не по дате-окну). FBO (`assembly_id=0`, ~11%) —
  импутация из FBS того же nm + наборы через `mix_data`. Покрытие 100%.
- **Ozon:** первые 2 сегмента `posting_number` → МС `customerorder` (агент «Покупатель Озон»),
  окно −45д. 3 слоя: точно → импутация по SKU → группа `offer_id`. Покрытие ~99%.
- **Яндекс:** `offerId` = наш артикул → МС-заказы «Покупатель Маркет», `Σ cost_seb×qty` по месяцам. 100%.

---

## 3. Витрина `margin_by_sku` — состав (юнит-экономика)

Строка = `article × platform × account × период (месяц)`. Все суммы — за период по этому SKU.

| Поле | Смысл |
|---|---|
| `article` | WB: nm_id · Ozon: sku |
| `platform`, `account` | `wb`/`ozon` × `wb_acc1/2`, `oz_acc1/2` |
| `period_from`, `period_to` | месяц (модель «период = дата формирования отчёта») |
| `qty` | штук продано (**WB заполнено; Ozon = NULL** — см. ниже) |
| `revenue_buyer` | выручка по цене покупателя (WB — уже после СПП) |
| `cogs` | себестоимость проданного (из МС-отгрузок) |
| `commission` | комиссия площадки |
| `logistics` | логистика |
| `returns_sum` | возвраты |
| `storage`, `acceptance`, `other` | хранение / приёмка / прочее |
| `net_profit` | чистая = to_pay − logistics − storage − acceptance − other − COGS |
| `margin_pct` | маржа, % |
| `spp_pct`, `commission_pct` | СПП% (WB) и комиссия% |

**Покрытие (на 2026-07-20):**
- `wb`  · `wb_acc1` — 2025-12…2026-07, 2604 SKU, qty 11217
- `wb`  · `wb_acc2` — 2025-12…2026-07, 1568 SKU, qty 6670
- `ozon`· `oz_acc1` — 2026-01…2026-07, 2058 SKU
- `ozon`· `oz_acc2` — 2026-01…2026-07, 1063 SKU

⚠️ **Ozon `qty` в этой витрине = NULL** → себест/шт по Ozon так не получить напрямую; для per-unit
Ozon брать штуки из `raw_ozon_posting`/транзакций (`items[]` повторяется по штукам). Для WB `qty` есть.

⚠️ **«Наша цена» vs «цена покупателя».** `revenue_buyer` для WB — цена ПОКУПАТЕЛЯ (после СПП).
Для юнит-экономики «по нашей цене» (сопоставимо между площадками) брать из таблицы **`sales`**:
`our_price`, `buyer_price`, `to_pay`, `revenue_wb`, поле `granularity` (недельная/месячная).

---

## 4. Готовый SQL — юнит-экономика на nm (WB)

```sql
-- последний доступный месяц, себест и юнит-экономика на штуку
SELECT DISTINCT ON (article)
       article                              AS nm_id,
       period_from,
       qty,
       round(revenue_buyer / NULLIF(qty,0)) AS rev_per_unit,
       round(cogs          / NULLIF(qty,0)) AS cogs_per_unit,
       round(commission    / NULLIF(qty,0)) AS comm_per_unit,
       round(logistics     / NULLIF(qty,0)) AS log_per_unit,
       round(net_profit    / NULLIF(qty,0)) AS net_per_unit,
       margin_pct, spp_pct
FROM margin_by_sku
WHERE platform='wb' AND account='wb_acc1'
  AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$'
ORDER BY article, period_from DESC;
```

Замещающая закупочная (для «а что если» по акциям):
```sql
SELECT article, buy_price FROM ms_product WHERE article = %s;
```

---

## 5. Расходы по площадкам — состав и источник

| Площадка | Источник сырья (в БД) | Состав расходов | Ловушки |
|---|---|---|---|
| **WB** | `raw_wb_report` (недельный отчёт реализации) | комиссия ~14% (от цены покупателя post-СПП), логистика `delivery_rub`, хранение/приёмка/штрафы, реклама. **СПП ~28–30% несёт продавец** | период = месяц **формирования** отчёта, не `rr_dt` (rr_dt — только недельная оперативка) |
| **Ozon** | `raw_ozon_transaction` (по дням) + `raw_ozon_posting` (цены) | комиссия ~40% (главная), реклама ~5% (рычаг), логистика ~6%, возвраты, штрафы, эквайринг, Premium | **двойной счёт** `amount` vs `services[]` — не суммировать оба |
| **Яндекс** | `raw_yandex_stats_order` + `raw_yandex_services` → `yandex_finance_monthly` | комиссия ~25% от (payment+subsidy), логистика, эквайринг, буст. Выручка = **payment+subsidy** без отмен | REFUND в `payments[]` идёт **с плюсом** → PAYMENT−REFUND |

Формула чистой везде: `net = к_перечислению − логистика − хранение − приёмка − прочее − COGS`.
Сверка МС↔площадка — **по деньгам**, не по штукам (набор = 1 юнит на площадке, N в МС).

---

## 5b. Реализованная vs ЗАМЕЩАЮЩАЯ себестоимость — для рекламы брать замещающую

Два разных числа, не путать (кейс nm `343261039`, проверено 2026-07-20):

| Концепт | Где | nm 343261039 | Для чего |
|---|---|---|---|
| Реализованная (что реально заплатили, FIFO) | `margin_by_sku.cogs` / `ms_demand_cogs` | 63–186 ₽/шт | P&L, фактическая прошлая прибыль |
| **Замещающая (сколько стоит перезакупить СЕЙЧАС)** | **`ms_product.buy_price`** | **638.99 ₽/шт** | **ДРР/ставки, форвардные ad-решения** |

⚠️ **Ловушка «мнимой гигантской маржи»:** историческая FIFO-себест часто в разы ниже текущей закупки
(здесь 63 ₽ против 639 ₏ — ×10, старые дешёвые приёмки). Если считать окупаемость рекламы на
исторической — маржа кажется 90%, а на замещающей это реалистичные ~40% COGS. **Для рекламы всегда
замещающая.** Это НЕ подмена товара (МС корректно отгружает по листингу заправку LH-W1580X) — это
эффект устаревшей FIFO. Диагноз «система взяла не тот товар» здесь ложный.

### Надёжный ключ WB nm → реальный товар МС = ПУТЬ ОТГРУЗКИ (не баркод, не externalCode)

Баркод и `externalCode` для многих nm пустые (для 343261039 оба вернули 0). Авторитетный ключ —
что МС **физически списал** по этой продаже (наборы уже разложены на компоненты):

```
raw_wb_report.assembly_id → ms_demand_cogs.demand_name(=demand_id) → ms_demand_pos.ms_id → ms_product
```

Готовый рецепт «nm → реальный товар + живая замещающая закупка» (доминирующий товар по штукам):

```sql
WITH nm_ship AS (
  SELECT w.payload->>'nm_id' nm, pos.ms_id, sum(pos.qty) q
  FROM raw_wb_report w
  JOIN ms_demand_cogs d  ON d.demand_name = w.payload->>'assembly_id'
  JOIN ms_demand_pos pos ON pos.demand_id = d.demand_id
  GROUP BY 1,2),
ranked AS (SELECT *, row_number() OVER (PARTITION BY nm ORDER BY q DESC) rn FROM nm_ship)
SELECT r.nm, p.article, p.name, p.buy_price AS replacement_cost
FROM ranked r JOIN ms_product p ON p.ms_id = r.ms_id
WHERE r.rn = 1;
```

Проверено: 343261039 → LH-W1580X → 638.99 ₽; 199569721 → SASP200HL (Sakura SP200HL) → 962.34 ₽.

**Коллектор закупки строить НЕ нужно** — `ms_product.buy_price` уже держит живую замещающую цену
(обновляется ежедневно `collectors/ms_products.py`). Нужна лишь связка nm→ms_id из пути отгрузки
(данные уже в БД: `ms_demand_cogs` + `ms_demand_pos`). Это можно оформить как **mkt-витрину**
(миграция 1xx, `run_marketing`), которая ЧИТАЕТ `ms_product`/`ms_demand_pos` и не трогает
`margin_by_sku` — граница fin/mkt соблюдена (mkt читает, не пишет COGS).

---

## 6. Куда смотреть в коде (fin-домен)

- Витрины: `reports/margin_by_sku.py` (WB+Ozon), `reports/margin_ozon_sku.py`, `reports/ozon_expenses.py`
- Себест-кэш: `collectors/ms_demand_cogs.py` → `ms_demand_cogs` / `ms_demand_pos`
- Закупочная/справочник: `collectors/ms_products.py` → `ms_product` / `ms_barcode`
- Оркестратор: `run_daily.py` (скользящее окно текущий+прошлый месяц, все аккаунты)
- Ручные факты себеста: `cogs_manual` (миграция 029)

**Контроль-эталон** (сверять, не подгонять): nmID `216421567`, май 2026 → чистая **3741.77 ₽**.

> Правки финансового кода — только в fin-сессии/ветке `fin/*` (territory_guard блокирует коммит
> fin-файлов из чужой ветки). Эта шпаргалка — read-only справочник, править витрину из mkt нельзя.

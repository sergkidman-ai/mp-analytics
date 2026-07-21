# MKT · WB: юнит-экономика и работа с финансовыми данными (инструкция)

> Для маркетинговой ветки (`mkt`). Цель: считать маржу/юнит-экономику по WB для решений по
> рекламным ставкам — **не пересчитывая с нуля**, а читая уже скачанное сырьё и готовые витрины.
> Всё в одной БД `mp_analytics`. Сверено с БД 2026-07-20. Дополняет `docs/FIN_DATA_FOR_MKT.md`.

---

## 0. Golden rules (прочитать первым)

1. **Юнит-экономика УЖЕ посчитана** — витрина `margin_by_sku` (SKU × месяц × площадка). Не пересчитывать.
2. **Себест для рекламы = ЗАМЕЩАЮЩАЯ** (`ms_product.buy_price`), не историческая. Историческая FIFO бывает
   в разы ниже текущей закупки → «мнимая гигантская маржа». Форвардное ad-решение считать на замещающей.
3. **Не считать метрики из ТЕКУЩЕГО (неполного) месяца** на уровне SKU — там 1–2 штуки, это шум.
   Брать последний ПОЛНЫЙ месяц или трейлинг 30–90 дней.
4. `margin_by_sku` пишет только `fin`; `mkt` читает read-only. Свои витрины строить можно (миграции 1xx),
   но они READ-ONLY по `ms_product`/`ms_demand_pos`/`raw_wb_report`, не трогают `margin_by_sku`.

---

## 1. Доступ к БД из worktree mkt

В worktree нет своего `venv`, `.env` — симлинк на корневой. Поэтому:

```bash
/opt/mp-analytics/venv/bin/python your_script.py     # общий venv
```
```python
import os, sys
from dotenv import load_dotenv
load_dotenv(os.path.join(os.getcwd(), ".env"))   # → /opt/mp-analytics/.env (DATABASE_URL)
sys.path.insert(0, "/opt/mp-analytics")           # core.db из основного дерева
from core import db
rows = db.query("SELECT ... FROM margin_by_sku WHERE platform='wb'")
```

---

## 2. Что уже скачано (не тратить время на сбор)

| Слой | Таблица | Что внутри | Период / объём |
|---|---|---|---|
| **Сырьё WB** | `raw_wb_report` | построчный отчёт реализации ВБ (продажи/возвраты/логистика), payload JSONB | 2025-12…2026-07, acc1 96.9k строк / acc2 56.4k |
| Витрина юнит-эк. | `margin_by_sku` | готовая экономика на SKU×месяц (COGS, комиссия, логистика, чистая, маржа%) | 2025-12…2026-07 |
| Витрина цен/продаж | `sales` | цены и суммы на SKU×месяц (наша цена, после СПП, к перечислению) | 2025-12…2026-07 |
| Себест продаж (факт) | `ms_demand_cogs` + `ms_demand_pos` | FIFO-себест отгрузок МС по документам и позициям | — |
| Замещающая закупка | `ms_product` (`buy_price`) | живая закупочная цена по артикулу МС | обновляется ежедневно |
| Ручные себесты | `cogs_manual` | точечные факты себеста | — |
| Остатки/поставщики | `supplier_stock` | сток, дни запаса, поставщик, себест | — |

**Для WB-юнит-экономики почти всегда хватает трёх: `margin_by_sku` + `sales` + `ms_product`.**
Сырьё `raw_wb_report` — когда нужна построчная детализация (траектория цены, СПП по сделкам, ключ отгрузки).

---

## 3. Готовые витрины — поля

### `margin_by_sku` (юнит-экономика, читать в первую очередь)
Строка = `article × platform × account × месяц`. Все суммы — за месяц по SKU.

| Поле | Смысл |
|---|---|
| `article` | **WB: nm_id** (числовой, = ключ Джема/позиций) · Ozon: sku |
| `platform`,`account` | `wb` × `wb_acc1/2` |
| `period_from`,`period_to` | месяц (модель «период = дата формирования отчёта») |
| `qty` | штук (WB заполнено) |
| `revenue_buyer` | ⚠️ **выручка по НАШЕЙ цене (ДО СПП)** — имя обманчиво, это не цена покупателя |
| `cogs` | себест проданного (реализованная, из МС-отгрузок) |
| `commission`,`logistics`,`storage`,`acceptance`,`returns_sum`,`other` | статьи расходов ВБ |
| `net_profit` | чистая = к_перечислению(после СПП) − логистика − хранение − приёмка − прочее − COGS |
| `margin_pct` | маржа, % |
| `spp_pct`,`commission_pct` | ⚠️ `spp_pct` часто **NULL** → считать СПП вручную (см. ниже) |

### `sales` (цены — читать когда нужна цена/СПП)
| Поле | Смысл |
|---|---|
| `our_price` | **наша цена ЗА ШТУКУ (до СПП)**, средняя за месяц |
| `revenue_buyer` | наша цена × qty (до СПП) — то же, что в margin_by_sku |
| `revenue_wb` | **после СПП** («ВБ реализовал», retail_amount) |
| `to_pay` | к перечислению продавцу |
| `commission`,`logistics`,`logistics_cnt`,`storage`,`acceptance`,`returns_sum`,`other` | расходы |
| `granularity` | `month` (месячные срезы) |

**СПП% = `1 − revenue_wb / revenue_buyer`** (в витрине отдельного поля нет).

---

## 4. Сырьё `raw_wb_report` — ключевые поля payload

Каждая строка = одна операция (`doc_type_name`: `Продажа` / `Возврат` / логистика). Доступ:
`payload->>'field'` (текст → приводить `::numeric` / `::date`).

| Поле | Смысл |
|---|---|
| `nm_id` | наш ключ SKU (= article в витринах) |
| `sa_name` | vendorCode; `barcode` — баркод; `brand_name`,`subject_name` |
| `quantity` | штук в операции |
| `doc_type_name` | `Продажа` / `Возврат` (возврат — со своим знаком) |
| `retail_price` | цена ДО скидки продавца (list) |
| `retail_price_withdisc_rub` | **наша цена после нашей скидки = our_price** (у нас скидок нет → = retail_price) |
| `retail_amount` | **цена покупателя ПОСЛЕ СПП** (что «ВБ реализовал») |
| `ppvz_spp_prc` | СПП, % (несёт продавец) |
| `ppvz_for_pay` | к перечислению продавцу по строке |
| `commission_percent` / `ppvz_sales_commission` | комиссия % / ₽ |
| `delivery_rub` | логистика ₽ |
| `storage_fee`,`acceptance`,`penalty`,`deduction` | хранение / приёмка / штраф / удержания |
| `acquiring_fee` | эквайринг ₽ |
| `cashback_amount` | баллы лояльности (вычитаются из перечисления) |
| `return_amount` | сумма возврата |
| `assembly_id` | **ключ к отгрузке МС → себест** (см. §6). `0` = FBO (отгрузки продавца нет) |
| `rr_dt` | дата реализации — для НЕДЕЛЬНОЙ оперативки |
| `create_dt` | дата формирования отчёта — для МЕСЯЧНОЙ модели |
| `realizationreport_id` | id недельного отчёта (весь отчёт = один период) |

### Три цены WB (запомнить)
```
retail_price (до скидки) ≥ retail_price_withdisc_rub (НАША цена) ≥ retail_amount (цена покупателя после СПП)
```
У нас скидок продавца нет → первые две равны. **СПП** = разница между нашей ценой и `retail_amount`,
её несёт продавец (плохо для нас). Рост СПП = меньше выручки.

---

## 5. Готовые SQL-рецепты

### 5.1 Юнит-экономика на nm (последний ПОЛНЫЙ месяц)
```sql
SELECT DISTINCT ON (article)
       article AS nm_id, period_from, qty,
       round(revenue_buyer/NULLIF(qty,0)) AS our_price_u,      -- наша цена/шт (до СПП)
       round(cogs/NULLIF(qty,0))          AS cogs_u,           -- реализованная себест/шт
       round(commission/NULLIF(qty,0))    AS comm_u,
       round(logistics/NULLIF(qty,0))     AS log_u,
       round(net_profit/NULLIF(qty,0))    AS net_u,
       margin_pct
FROM margin_by_sku
WHERE platform='wb' AND account='wb_acc1'
  AND period_from = '2026-06-01'            -- ПОЛНЫЙ месяц, не текущий
  AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$'
ORDER BY article, period_from DESC;
```

### 5.2 Замещающая себест на nm (для форвардных ad-решений)
Ключ nm→реальный товар = ПУТЬ ОТГРУЗКИ (не баркод/externalCode — они часто пусты):
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
Форвардная маржа/шт = `our_price_u × (1 − СПП) − replacement_cost − comm_u − log_u`.

### 5.3 Траектория цены по месяцам (диагностика «почему цена выросла»)
```sql
SELECT to_char(period_from,'YYYY-MM') mon, qty,
       round(revenue_buyer/NULLIF(qty,0)) our_u,
       round(revenue_wb   /NULLIF(qty,0)) after_spp_u,
       round(100.0*(1-revenue_wb/NULLIF(revenue_buyer,0))) spp_pct
FROM sales
WHERE platform='wb' AND article=%s AND granularity='month'
ORDER BY period_from;
```

---

## 6. Себестоимость: реализованная vs замещающая

| Концепт | Где | Для чего |
|---|---|---|
| Реализованная (что реально заплатили, FIFO) | `margin_by_sku.cogs` | факт-P&L, прошлая прибыль |
| **Замещающая (перезакупить сейчас)** | `ms_product.buy_price` (через путь §5.2) | **ДРР/ставки, форвард** |

⚠️ Разрыв бывает ×10 (nm 343261039: FIFO 63 ₽ / витрина 186 ₽ vs замещающая 639 ₽). Для рекламы — замещающая.
Это НЕ подмена товара: МС корректно отгружает по листингу заправку LH-W1580X; «взяло не тот товар» — ложный
диагноз, причина — устаревшая FIFO. **Коллектор закупки строить не нужно** — `ms_product.buy_price` уже живой.

---

## 7. Ловушки измерения (иначе получишь ложные выводы)

1. **Неполный текущий месяц + модель «дата формирования».** У SKU в текущем месяце пока 1–2 штуки →
   «средняя цена» = одна сделка, шум. **Симптом, который уже поймали:** «цены июля выросли на >13%» —
   артефакт: на ПОЛНЫХ май→июнь рост цены медиана +2%, а «+67%» появляется только против неполного июля,
   где 90 из 92 «выросших» SKU имеют qty<4. **Правило: сравнивать полные месяцы, тонкий хвост (qty 1–2)
   из решений исключать.**
2. **`revenue_buyer` ≠ цена покупателя** — это наша цена до СПП. Цена покупателя = `revenue_wb`.
3. **`our_price` в `sales` — уже за штуку** (не суммировать/делить на qty повторно). Для среднего за период
   брать `sum(revenue_buyer)/sum(qty)`.
4. **`spp_pct` в margin_by_sku часто NULL** → СПП считать `1 − revenue_wb/revenue_buyer`.
5. **Возвраты** (`doc_type_name='Возврат'`, `return_amount`) идут со своим знаком — не терять при агрегации сырья.
6. **FBO** (`assembly_id='0'`, ~11%) — отгрузки продавца нет; себест в витрине через импутацию. Ключ §5.2
   для FBO не сработает — брать `margin_by_sku.cogs` как есть.

---

## 8. Алгоритм «на какие SKU поднять ставку» (сводно)

1. Взять SKU с реальным объёмом за **последний полный месяц** (`qty >= 5`).
2. Цена/шт — `revenue_buyer/qty`; СПП — `1 − revenue_wb/revenue_buyer`.
3. Себест — **замещающая** `ms_product.buy_price` (§5.2), не витринная FIFO.
4. Форвардная маржа/шт = `цена×(1−СПП) − замещающая − комиссия/шт − логистика/шт`.
5. Поднимать ставку там, где маржа/шт здоровая И есть живой объём; тонкий хвост (qty 1–2) игнорировать.
6. ДРР-порог = маржа/шт ÷ цена (сколько можно отдать в рекламу, не уходя в минус).

---

**Контроль-эталон fin** (сверять расчёты): nmID `216421567`, май 2026 → чистая **3741.77 ₽**.
Связанные документы: `docs/FIN_DATA_FOR_MKT.md` (общая финмодель + расходы по всем площадкам),
`docs/BRIEF_MKT.md` (бриф домена mkt).

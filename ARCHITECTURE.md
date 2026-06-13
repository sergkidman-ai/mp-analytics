# ARCHITECTURE.md — BI-система «Пульт бизнеса» (маркетплейс-аналитика)

> Карта проекта для Claude Code. Читать перед любой задачей.
> Документ-видение ВСЕГО продукта + поэтапная дорожная карта.
> Строим слоями: каждый этап даёт рабочий результат сам по себе.

---

## 1. Видение продукта

Единая система бизнес-аналитики для продавца картриджей (бренд «Цифровой квадрат»).
Цель — «пульт управления бизнесом»: забрать по API ВСЁ, что отдают источники, свести
в одну базу, считать реальную экономику каждого товара и помогать принимать решения
(поднять цену / вывести / масштабировать / где просадка и почему).

Источники данных (8 точек сбора):
- МойСклад — центральный справочник: товары, артикулы, себестоимость, остатки, закупки
- Wildberries x2 аккаунта — продажи, финансы, реклама, позиции в выдаче, воронка
- Ozon x2 аккаунта — то же
- Яндекс Маркет x2 аккаунта — то же

Что система показывает (домены аналитики):
- Экономика: реальная маржа по SKU после ВСЕХ расходов, прибыль за человеко-час
- Продажи: динамика по аккаунтам, детекция просадок (день/неделя/год) + причина
- Видимость: позиции SKU в поисковой выдаче, динамика
- Реклама: расходы, ДРР, ROI по кампаниям
- Расходы: комиссия, логистика, хранение, приёмка, возвраты, прочее
- Стратегия: ABC (маржа x трафик), локомотивы/хвост/балласт, сравнение площадок

Витрина: веб-дашборд + автоанализ Claude (объяснение просадок и сигналов).

---

## 2. Принципы (соблюдать всегда)

1. МойСклад — источник правды по товарам и себестоимости. Артикул (code/article) —
   единый ключ связи между МойСклад и всеми маркетплейс-аккаунтами.
2. Сырьё отдельно от расчётов. raw-слой хранит ответы API как есть (JSONB). Любой расчёт
   пересобирается из сырья без повторного обращения к API.
3. Бизнес-логика в одном месте (core/economics.py). Формулы не дублируются по площадкам.
4. Идемпотентность. Повторный сбор за период не создаёт дублей — UPSERT по натуральному ключу.
5. Каждый шаг проверяется на реальности. Контроль WB: SKU 00024 (nmID 216421567) за май
   2026 → чистая прибыль ≈ 3909 руб при 61 продаже (без труда).
6. Аккаунты различаются полем account, площадка — полем platform.
7. Секреты — только в .env (gitignored), никогда в коде и не выводить в чат.
8. Чужие проекты не трогать: /opt/sokol-server, /opt/tz-analyzer-src, /var/www/sokol.
   Проект живёт только в /opt/mp-analytics.

---

## 3. Инфраструктура (УЖЕ РАЗВЁРНУТА)

- Сервер: Ubuntu 22.04, Python 3.10, Docker 29
- БД: PostgreSQL 16 в Docker, контейнер mp-postgres, порт 127.0.0.1:5433, база mp_analytics,
  том на диске /opt/mp-analytics/pgdata (данные переживают рестарт)
- Python: venv в /opt/mp-analytics/venv (requests, psycopg2-binary, SQLAlchemy, pandas,
  python-dotenv, openpyxl)
- Доступ к БД: DATABASE_URL в /opt/mp-analytics/.env
- git: репозиторий в /opt/mp-analytics, ветка main

---

## 4. Структура репозитория

```
/opt/mp-analytics/
├── collectors/
│   ├── moysklad.py      # МойСклад: товары, себестоимость, остатки, закупки
│   ├── wb.py            # WB: продажи, финансы, реклама, позиции, воронка
│   ├── ozon.py          # Ozon
│   └── yandex.py        # Яндекс Маркет
├── core/
│   ├── db.py            # подключение, схема, UPSERT-хелперы
│   ├── economics.py     # юнит-экономика (формулы — раздел 7)
│   ├── normalize.py     # raw → clean
│   └── anomalies.py     # детектор просадок (раздел 8)
├── reports/
│   ├── margin_by_sku.py
│   ├── sales_dynamics.py
│   └── abc.py
├── ai/
│   └── analyst.py       # автоанализ через Anthropic API (позже)
├── web/                 # веб-дашборд (позже)
├── migrations/          # SQL-схема (DDL)
├── config.py            # константы (раздел 7)
├── .env                 # секреты (НЕ в git)
├── run_daily.py         # оркестратор (cron)
└── ARCHITECTURE.md
```

---

## 5. Модель данных (PostgreSQL)

### Слой 1 — RAW (сырьё как пришло из API)

```sql
CREATE TABLE raw_moysklad_product (
    id BIGSERIAL PRIMARY KEY, ms_id TEXT, article TEXT,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(ms_id)
);
CREATE TABLE raw_wb_report (
    id BIGSERIAL PRIMARY KEY, account TEXT, rrd_id BIGINT,
    period_from DATE, period_to DATE,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, rrd_id)
);
CREATE TABLE raw_ozon_transaction (
    id BIGSERIAL PRIMARY KEY, account TEXT, operation_id BIGINT,
    period_from DATE, period_to DATE,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, operation_id)
);
CREATE TABLE raw_yandex_order (
    id BIGSERIAL PRIMARY KEY, account TEXT, order_id BIGINT, item_id BIGINT,
    period_from DATE, period_to DATE,
    payload JSONB NOT NULL, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account, order_id, item_id)
);
CREATE TABLE raw_positions (
    id BIGSERIAL PRIMARY KEY, platform TEXT, account TEXT,
    article TEXT, keyword TEXT, position INT, captured_at DATE,
    payload JSONB, loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(platform, account, article, keyword, captured_at)
);
```

### Слой 2 — CLEAN (единый вид)

```sql
-- Справочник товаров (из МойСклад — источник правды)
CREATE TABLE products (
    article TEXT PRIMARY KEY,         -- единый артикул (code/article)
    ms_id TEXT, title TEXT, category TEXT,
    buy_price NUMERIC,                -- себестоимость за единицу (из МойСклад)
    length_cm NUMERIC, width_cm NUMERIC, height_cm NUMERIC,
    weight_kg NUMERIC, volume_l NUMERIC,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE stocks (
    article TEXT, source TEXT,        -- moysklad|wb|ozon|yandex
    account TEXT, qty NUMERIC, captured_at DATE,
    PRIMARY KEY(article, source, account, captured_at)
);

CREATE TABLE sales (
    id BIGSERIAL PRIMARY KEY,
    article TEXT NOT NULL, platform TEXT NOT NULL, account TEXT NOT NULL,
    period_from DATE NOT NULL, period_to DATE NOT NULL,
    granularity TEXT NOT NULL,        -- day|week|month
    qty NUMERIC DEFAULT 0,
    our_price NUMERIC, buyer_price NUMERIC,
    revenue_buyer NUMERIC DEFAULT 0, to_pay NUMERIC DEFAULT 0,
    commission NUMERIC DEFAULT 0, logistics NUMERIC DEFAULT 0,
    logistics_cnt NUMERIC DEFAULT 0, returns_sum NUMERIC DEFAULT 0,
    storage NUMERIC DEFAULT 0, acceptance NUMERIC DEFAULT 0, other NUMERIC DEFAULT 0,
    loaded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(article, platform, account, period_from, period_to, granularity)
);
CREATE INDEX idx_sales_article ON sales(article);
CREATE INDEX idx_sales_period ON sales(period_from, period_to);

CREATE TABLE funnel (
    article TEXT, platform TEXT, account TEXT,
    period_from DATE, period_to DATE, granularity TEXT,
    views NUMERIC, clicks NUMERIC, add_to_cart NUMERIC,
    orders NUMERIC, open_card NUMERIC, cr NUMERIC,
    PRIMARY KEY(article, platform, account, period_from, period_to, granularity)
);

CREATE TABLE ads (
    article TEXT, platform TEXT, account TEXT,
    campaign TEXT, period_from DATE, period_to DATE,
    views NUMERIC, clicks NUMERIC, spend NUMERIC,
    orders NUMERIC, revenue NUMERIC, drr NUMERIC,
    PRIMARY KEY(article, platform, account, campaign, period_from, period_to)
);
```

### Слой 3 — MARTS (витрины, пересчитываются)

```sql
CREATE TABLE margin_by_sku (
    article TEXT, platform TEXT, account TEXT,
    period_from DATE, period_to DATE,
    qty NUMERIC, revenue_buyer NUMERIC, cogs NUMERIC,
    commission NUMERIC, logistics NUMERIC, returns_sum NUMERIC,
    storage NUMERIC, acceptance NUMERIC, other NUMERIC,
    net_profit NUMERIC, margin_pct NUMERIC,
    spp_pct NUMERIC, commission_pct NUMERIC,
    computed_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY(article, platform, account, period_from, period_to)
);

CREATE TABLE drops (
    article TEXT, platform TEXT, account TEXT,
    metric TEXT,                      -- revenue|orders|views|cr
    horizon TEXT,                     -- day|week|year
    current_val NUMERIC, prev_val NUMERIC, change_pct NUMERIC,
    likely_cause TEXT,
    detected_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY(article, platform, account, metric, horizon, detected_at)
);
```

---

## 6. Спецификация источников

### 6.1 МойСклад (collectors/moysklad.py) — ПЕРВЫЙ

Авторизация: Authorization: Bearer <token>. Base: https://api.moysklad.ru/api/remap/1.2
Лимит ~45 запросов/3 сек, пагинация limit/offset (max limit 1000).

| Назначение | Эндпоинт |
|---|---|
| Товары | GET /entity/product?limit=1000&offset= |
| Себестоимость | поле buyPrice в товаре, либо GET /report/stock/all |
| Остатки | GET /report/stock/all или /report/stock/bystore |
| Закупки | GET /entity/purchaseorder или /entity/supply |

Маппинг товара → products: article→article (ключ), id→ms_id, name→title,
buyPrice.value/100→buy_price, габариты если есть.
ВНИМАНИЕ: цены в МойСклад в КОПЕЙКАХ — делить на 100.

### 6.2 Wildberries (collectors/wb.py)

Авторизация: Authorization: <token> (категория «Финансы» после 15.07.2026).

| Назначение | URL | Метод |
|---|---|---|
| Финотчёт | statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod?dateFrom=&dateTo=&limit=5000&rrdid= | GET |
| Карточки/габариты | content-api.wildberries.ru/content/v2/get/cards/list | POST |
| Воронка | seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products | POST |
| Реклама | advert-api.wildberries.ru/adv/v3/fullstats | GET |
| Тарифы складов | common-api.wildberries.ru/api/v1/tariffs/box?date= | GET |

Поля финотчёта: nm_id, doc_type_name(Продажа/Возврат), supplier_oper_name(Продажа/Логистика),
quantity→qty, retail_price_withdisc_rub→our_price, retail_amount→buyer_price/revenue_buyer,
ppvz_for_pay→to_pay, delivery_rub→logistics, delivery_amount→logistics_cnt,
storage_fee→storage, deduction→other, acceptance→acceptance.
Пагинация: rrdid = последний rrd_id, стоп при < limit. Связь nm_id→article через карточки.

Чтение xlsx-отчётов WB (если из файлов): НЕ iter_rows/read_only (ломаются — 1 колонка).
Только load_workbook(f, data_only=True) + ws.cell(row=r, column=c).

### 6.3 Ozon (collectors/ozon.py)

Авторизация: Client-Id, Api-Key.
| Назначение | URL |
|---|---|
| Финтранзакции | POST api-seller.ozon.ru/v3/finance/transaction/list |
| Товары | POST api-seller.ozon.ru/v3/product/info/list |
| Остатки | POST api-seller.ozon.ru/v4/product/info/stocks |
| Аналитика/позиции | POST api-seller.ozon.ru/v1/analytics/data |
Расходы по operation_type. Сумма по offer_id(=article). operation_id — ключ raw.

### 6.4 Яндекс Маркет (collectors/yandex.py)

Авторизация: Authorization: Bearer <oauth>.
| Назначение | URL |
|---|---|
| Заказы | POST api.partner.market.yandex.ru/campaigns/{id}/stats/orders |
| Отчёты/комиссии | через report API |
Уточнять актуальные эндпоинты в доке — API Яндекса меняется чаще.

---

## 7. Бизнес-логика (config.py + core/economics.py)

Выведено из реальных данных мая 2026 (5 финотчётов WB, 1807 продаж).

```python
LOGISTICS_BASE = 51.0        # руб базовая часть
LOGISTICS_PER_LITER = 24.5   # руб за литр
# Логистика = LOGISTICS_BASE + LOGISTICS_PER_LITER * объём_л
# Проверка: 7 л → 222 руб (калькулятор давал 220). НЕ использовать старые 33+8.
WB_COMMISSION_PCT = 13.71    # комиссия+эквайринг от выручки покупателя (среднее)
WB_SPP_PCT = 28.84           # скидка WB от нашей цены
WB_RETURNS_PCT = 1.68
WB_OTHER_PCT = 3.88
MIN_SAMPLE = 3
PACKER_WAGE_HOUR = 475.0     # ставка упаковщика (3800/8ч)
```

Чистая прибыль (маржа без труда):
```
net_profit = revenue_buyer - buy_price*qty - commission - logistics
           - returns_sum - storage - acceptance - other
```
revenue_buyer = цена покупателя ПОСЛЕ скидок площадки. ROI — от цены площадки, НЕ от нашей.

Три цены WB: наша цена → минус СПП(~28.84% от нашей, платит WB) → цена WB(=цена покупателя)
→ минус комиссия(~13.71% от цены WB) → к перечислению. Комиссия плавает 3-18%/нед, ~13.7% средн.

Прибыль за человеко-час (контур 2, после замеров времени по габаритным группам):
(net_profit − labor_cost)/labor_hours, labor_cost = время × PACKER_WAGE_HOUR.
Пример SKU 00024: при 7 мин/заказ прибыль после труда падает с 64 до 9 руб/шт.

Проверка габаритов: картриджи 0.3-5 л. Подозрение если объём > 10-20 л,
плотность(weight/volume) вне 0.05-3 кг/л, сторона > 50 см. Плотность надёжнее объёма.

---

## 8. Детектор просадок (core/anomalies.py)

Сравнение по трём горизонтам для каждого (article, platform, account):
- день к дню (вчера vs позавчера) — быстро, шумно
- неделя к неделе (эта vs прошлая)
- год/месяц (этот период vs аналог год/месяц назад) — сезонность

Метрики: revenue, orders, views, cr. Сигнал при падении > порога (старт: -15%).
Запись в drops. likely_cause заполняется логикой:
- упали views → потеря видимости/позиций
- упал cr при тех же views → проблема цены/карточки/конкурента
- qty=0 при спросе → кончился остаток (проверить stocks)
- вырос buyer_price → срезали СПП
- ads.spend=0 был >0 → отключилась реклама
Финальное «почему» формулирует Claude (раздел 9).

---

## 9. Автоанализ Claude (ai/analyst.py) — позже

Схема А (старт): дашборд формирует сводку просадок → пользователь приносит в чат Claude →
анализ вручную. Работает сразу.
Схема Б (автоматизация): скрипт берёт drops + связку метрик, шлёт в Anthropic API
(api.anthropic.com/v1/messages), пишет причину в drops.likely_cause и дашборд.
Требует ANTHROPIC_API_KEY в .env. Модель claude-sonnet.

---

## 10. Дорожная карта (порядок для Claude Code)

Каждый этап — рабочий результат. Не начинать следующий, пока текущий не проверен.

Этап 0 — каркас БД. core/db.py + миграции (все таблицы раздела 5). Применить схему.
Этап 1 — МойСклад. collectors/moysklad.py → raw → products (товары + себестоимость).
  Контроль: число товаров = кабинету МойСклад; у SKU 00024 buy_price = 273.
Этап 2 — WB финансы. collectors/wb.py (финотчёт + карточки) → raw → sales.
  Контроль: SKU 00024 за май → net_profit ≈ 3909 руб, комиссия 13.71%, СПП 28.84%.
Этап 3 — витрина маржи. reports/margin_by_sku.py. Сверить с контрольными точками.
Этап 4 — WB воронка + реклама + позиции. funnel, ads, raw_positions.
Этап 5 — детектор просадок. core/anomalies.py → drops (3 горизонта).
Этап 6 — Ozon. Коллектор → raw → sales/funnel. Витрины по 2 площадкам.
Этап 7 — Яндекс. Аналогично. 3 площадки.
Этап 8 — вторые аккаунты. Параметризация по account. Все 8 точек.
Этап 9 — веб-дашборд. Аккаунты, светофор просадок, маржа, позиции, реклама.
Этап 10 — автоанализ Claude (Схема Б). ai/analyst.py.
Этап 11 — расширения. ABC (маржа x трафик), человеко-час, оборачиваемость.

---

## 11. Контрольные точки (сверка с реальностью)

| Что | Ожидаемое | Источник |
|---|---|---|
| SKU 00024 себестоимость | 273 руб | МойСклад |
| SKU 00024 маржа за май (WB, без труда) | ≈ 3909 руб при 61 продаже | 5 финотчётов мая |
| Комиссия WB средняя | 13.71% | те же |
| СПП средняя | 28.84% | те же |
| Логистика 7 л | ≈ 220-222 руб | калькулятор + регрессия |
| Логистика на доставку средняя | ≈ 269 руб | факт мая, 2040 доставок |

---

## 12. Секреты (.env, НЕ в git, НЕ выводить в чат)

```
DATABASE_URL=postgresql://mp_user:***@127.0.0.1:5433/mp_analytics
MOYSKLAD_TOKEN=...
WB_TOKEN_ACC1=...
WB_TOKEN_ACC2=...
OZON_CLIENT_ID_ACC1=...  OZON_API_KEY_ACC1=...
OZON_CLIENT_ID_ACC2=...  OZON_API_KEY_ACC2=...
YANDEX_OAUTH_ACC1=...  YANDEX_CAMPAIGN_ACC1=...
YANDEX_OAUTH_ACC2=...  YANDEX_CAMPAIGN_ACC2=...
ANTHROPIC_API_KEY=...   # автоанализ (этап 10)
```

---

## 13. Предупреждения

- WB reportDetailByPeriod v5 отключается 15 июля 2026. Новая версия: токен «Финансы»,
  camelCase-поля, суммы строками, поле title. Изолировать парсинг полей в одной функции.
- МойСклад цены в копейках — делить на 100.
- Идемпотентность — только UPSERT, без дублей.
- Лаг финотчётов 2-3 недели — свежие периоды неполные, перекрывать сбор (±35 дней).
- Логистика только из факта или формулы 51+24.5, никогда 33+8.
- Связка по article — если на площадке артикул отличается, нужна таблица соответствий.
- Все деньги хранить числом в БД, нормализовать на этапе нормализации.

#!/usr/bin/env python3
# поток: mkt
"""Сторож автоакций WB: сразу после старта акции вывести из неё убыточные товары.

ЗАЧЕМ. Перед автоакцией мы считаем, кто в неё проходит по текущим закупочным ценам, и
грузим решение в ЛК. ВБ всё равно ночью срезает цены отдельным карточкам вплоть до −92 %.
Этот скрипт запускается сразу после старта акции, находит товары, которые продаются ниже
нашего пола, и поднимает им цену до пола — тем самым выводя их из акции.

ПОЛ (правило Сергея, 10.09.2026):
    floor_net   = max(300 ₽; 10 % от себестоимости)          — чистая до налога и опер. расходов
    floor_price = (floor_net + cogs) / (1 − retention)
    retention   — доля удержаний МП от оборота со страницы «Отчёты МП» за последний месяц.
Пол применяется к цене ПОСЛЕ акционной скидки, но ДО СПП (`discountedPrice` Prices API):
именно она — база комиссии и знаменатель retention (см. `retention()` в price_quarantine.py).

СЕБЕСТОИМОСТЬ — две ветки, по остатку в МойСклад:
  • есть остаток на наших складах «Звездный»/«Дисквер» → себестоимость закупленного
    (`supplier_stock.cost_seb`, средневзвешенная по этим складам);
  • остаток только на «Удаленный склад» (склад поставщика) → минимальная цена среди
    поставщиков: прайсы `prc_price_row`, цена ТК и `supplier_stock.buy_price` — что дешевле.
Фолбэк — FIFO из `mkt_margin_control.fifo_cogs_u`. Нет себестоимости — товар выводим
из акции возвратом к доакционной цене (решение Сергея: «выводим»).

ПОЧЕМУ НЕ СПРАШИВАЕМ У ВБ, КТО В АКЦИИ. `/api/v1/calendar/promotions/nomenclatures`
отдаёт плановую цену `planPrice` только для акций `type=regular`; на `type=auto`
(наш случай) он отвечает 422 — проверено живьём 10.09.2026 на 7 акциях. Поэтому
максимально допустимой ценой участия считаем ту цену, которую ВБ выставил сам на старте:
автоакция и есть механизм доведения карточки до плановой цены. Отсюда правило:
цена ВБ ≥ нашего пола → не трогаем (мы уже в акции по максимально дорогой допустимой цене);
цена ВБ < пола → ставим пол, товар из акции выпадает. «Минус 1 ₽» тут не нужен: цену
назначил сам ВБ, мы её не превышаем.

ВОЛНЫ. ВБ доводит скидки не мгновенно и может вернуть товар в акцию, поэтому прогон
идёт трижды: T+15 мин, T+2 ч, T+8 ч (`--wave start|h2|h8`). Доакционную цену фиксируем
на первой волне в `wb_promo_guard_log` — из `wb_price` её потом не взять, коллектор в
09:20 перезапишет снимок акционными ценами.

КАРАНТИН. Резкий подъём цены ВБ может отправить в карантин (втрое ниже прежней —
известный порог на снижение). `ops/price_quarantine.py` этому не мешает и не откатывает:
цель он берёт из самой карточки карантина, то есть из нашей же новой цены, и лестницей
дотолкает её вверх. Отдельной синхронизации не нужно.

    ./venv/bin/python -m ops.wb_promo_guard --dry               # что бы сделал (по умолчанию)
    ./venv/bin/python -m ops.wb_promo_guard --dry --limit 20    # первый безопасный взгляд
    ./venv/bin/python -m ops.wb_promo_guard --apply --wave start
    ./venv/bin/python -m ops.wb_promo_guard --apply --acc wb_acc2 --no-tg
"""
import argparse
import csv
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mp-analytics")
load_dotenv("/opt/mp-analytics/.env")
from core import db  # noqa: E402

MSK = timezone(timedelta(hours=3))
WB_API = "https://discounts-prices-api.wildberries.ru"
CAL_API = "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_PRICES_ACC1", "wb_acc2": "WB_TOKEN_PRICES_ACC2"}
ACC_NAME = {"wb_acc1": "Цифровой квадрат", "wb_acc2": "Дисквэр"}

# Доля удержаний МП от оборота, страница «Отчёты МП» за август-2026 (значения дал Сергей).
# ОБНОВЛЯТЬ ЕЖЕМЕСЯЧНО: с новым закрытым месяцем цифры едут, а от них зависит весь пол.
RETENTION = {"wb_acc1": 0.569, "wb_acc2": 0.554}

MIN_NET = 300.0        # ₽ чистой на единицу — нижняя планка
MIN_NET_PCT = 0.10     # либо 10 % от себестоимости, что больше
DROP_EPS = 0.95        # «цена упала» = ниже доакционной хотя бы на 5 %
BASELINE_WINDOW = 5    # дней назад ищем базлайн-снимок относительно старта акции
PAGE = 1000
PACE = 0.35            # лимит WB: 10 запросов / 6 с на эндпоинт
PUSH_CHUNK = 100       # товаров в одной задаче /upload/task
COGS_MIN_KNOWN = 3000  # ниже этого справочник считается несобранным, кабинет пропускается
# Правило Сергея от 12.09.2026 (уточнено им же в тот же день): остаток поставщика в 4 штуки ЕЩЁ
# считается остатком, 3 и меньше — уже нет. Такой хвост разберут раньше, чем мы успеем докупить,
# поэтому предложение выбрасывается целиком: ни цену с него не берём, ни наличие по нему
# не признаём. Пример 6593: у Одиссея 3 шт по 4 766 ₽ и у Феррета 1 шт по 6 818 ₽ — оба хвоста
# отсекаются, закупочная становится 13 238 ₽ от Картридж Трейд, то есть пол акции был занижен
# втрое. Порог сравнивается как >=, поэтому значение — минимальный ПРИЕМЛЕМЫЙ остаток.
OWN_COST_MIN_SHARE = 0.5   # свой себест ниже половины рыночной цены — не верим (замена брака за 1 ₽)
MIN_SUP_STOCK = 4
FUNNEL_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
FUNNEL_TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}  # скоуп «Аналитика»
MP_MIN_CARDS = 5000    # ниже этого выдача воронки неполная — правило по остатку не применяем

NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()
REPORTS = "/opt/mp-analytics/docs/reports"


def token(acc):
    t = os.getenv(TOKEN_ENV[acc])
    if not t:
        raise RuntimeError(f"{TOKEN_ENV[acc]} не задан в .env (скоуп «Цены и скидки»)")
    return t


def head(acc):
    return {"Authorization": token(acc), "Content-Type": "application/json"}


def wb_get(acc, url, params, tries=5):
    """GET с пейсингом и отступом: 429 и 5xx у ВБ проходят сами, ронять прогон из-за них нельзя."""
    last = None
    for attempt in range(tries):
        time.sleep(PACE)
        try:
            r = requests.get(url, headers=head(acc), params=params, timeout=90)
        except requests.RequestException as e:
            last = e
            time.sleep(2 ** attempt * 3)
            continue
        if r.status_code != 429 and r.status_code < 500:
            r.raise_for_status()
            return r.json()
        last = requests.HTTPError(f"HTTP {r.status_code} {r.text[:120]}", response=r)
        time.sleep(2 ** attempt * 3)
    raise last


def fetch_goods(acc):
    """Живые цены всего каталога: {nm_id: (base_price, discount_pct, discounted_price, vendor_code)}.

    Берём именно живой API, а не витрину wb_price: витрина снимается в 09:20/16:20 МСК,
    а дно автоакции наступает ночью — на снимке его не видно.
    """
    out, offset = {}, 0
    while True:
        d = wb_get(acc, WB_API + "/api/v2/list/goods/filter", {"limit": PAGE, "offset": offset})
        goods = (d.get("data") or {}).get("listGoods") or []
        if not goods:
            break
        for g in goods:
            s = (g.get("sizes") or [{}])[0]
            nm = g.get("nmID")
            if nm is None:
                continue
            out[int(nm)] = (float(s.get("price") or 0), float(g.get("discount") or 0),
                            float(s.get("discountedPrice") or 0), g.get("vendorCode"))
        offset += PAGE
        if len(goods) < PAGE:
            break
    return out


def active_promos(acc):
    """id/названия акций, идущих прямо сейчас. Только для метки в журнале — сбой не критичен."""
    now = datetime.now(timezone.utc)
    try:
        d = wb_get(acc, CAL_API, {"startDateTime": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "endDateTime": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "allPromo": "false", "limit": 100})
    except Exception as e:
        print(f"  [{acc}] календарь недоступен: {str(e)[:120]}", flush=True)
        return [], None
    out, first_start = [], None
    for p in (d.get("data") or {}).get("promotions") or []:
        st = p.get("startDateTime")
        en = p.get("endDateTime")
        try:
            sd = datetime.fromisoformat(st.replace("Z", "+00:00"))
            ed = datetime.fromisoformat(en.replace("Z", "+00:00"))
        except Exception:
            continue
        if sd <= now <= ed:
            out.append((p.get("id"), p.get("name"), sd))
            if first_start is None or sd > first_start:
                first_start = sd            # самая свежая из идущих — она и порезала цены
    return out, first_start


def norm_code(code):
    """Производный код → основной: длиннее 4 символов → первые 4.

    Основной код товара — всегда ровно 4 знака, всё остальное производное от него, поэтому
    себестоимость ищется исключительно по 4-значному коду. Порог совпадает с каноном проекта
    (`reports/margin_control.py`). Код — строка от начала и до конца: ведущий ноль значащий
    ('0727' ≠ '727'), приводить к int нельзя нигде по цепочке.
    """
    code = (code or "").strip()
    return code[:4] if len(code) > 4 else (code or None)


def sets_map():
    """{код набора: {код компонента: количество}} из справочника set_cost.

    Количество = число вхождений кода в массив components (отдельной колонки нет).

    В components лежат ДВА формата. Исторический — голый 4-значный код. С 10.09.2026 API
    mix_data стал отдавать объекты, и коллектор записал в массив строковые представления
    dict-ов ("{'external_code': '7088', ...}") у 62 наборов из 993. Раньше такие строки
    просто отбрасывались — и набор целиком исчезал из справочника, превращаясь в обычный
    товар. А у набора собственных строк в supplier_stock не бывает, поэтому он попадал в
    ветку «остатка нет нигде» и выводился из акции. Так 11.09.2026 уехали 7092, 3152, 7003,
    3156 — при живых остатках у поставщиков. Теперь такая строка разбирается, а не теряется.
    """
    out = {}
    for r in db.query("SELECT external_code, components FROM set_cost"):
        comps = {}
        for c in (r["components"] or []):
            if not c:
                continue
            if len(c) != 4:                          # строковый dict от нового API
                m = re.search(r"'external_code':\s*'([^']{4})'", c)
                c = m.group(1) if m else None
                if not c:
                    continue
            comps[c] = comps.get(c, 0) + 1
        if comps:
            out[r["external_code"]] = comps
    return out


def links_map():
    """{код: множество связанных кодов} — универсальные модели.

    Два источника, объединяем: `prc_tc_link` (каталог ТК, incoming/outgoing references,
    1270 пар) и ручной атрибут МойСклада «Связь» (80 карточек; значение — код или список
    через точку с запятой). Связь считаем двусторонней: в prc_tc_link флаги направления
    расходятся, а для вопроса «тот же товар другой моделью» направление роли не играет.
    """
    out = {}

    def tie(a, b):
        a, b = norm_code(a), norm_code(b)
        if not a or not b or a == b:
            return
        out.setdefault(a, set()).add(b)
        out.setdefault(b, set()).add(a)

    for r in db.query("SELECT external_code, ref_code FROM prc_tc_link"):
        tie(r["external_code"], r["ref_code"])
    for r in db.query("""SELECT p.payload->>'externalCode' AS ec, a->>'value' AS v
                         FROM raw_moysklad_product p
                         CROSS JOIN LATERAL jsonb_array_elements(p.payload->'attributes') a
                         WHERE jsonb_typeof(p.payload->'attributes') = 'array'
                           AND a->>'name' = 'Связь'"""):
        for x in (r["v"] or "").replace(",", ";").split(";"):
            tie(r["ec"], x.strip())
    return out


def stock_map():
    """{основной 4-значный код: остатки и цены} по последнему снимку МС и всем прайсам.

    Всё сводится к 4-значному коду прямо в SQL (`left(external_code, 4)`): производные коды
    несут остаток и цену того же товара, и складывать их надо в родителя. Без этого теряется
    163 строки остатков и 6679 строк прайсов — у поставщиков коды почти всегда с суффиксом
    ('0002/18cs', '0001gp'). Ведущий ноль значащий, код везде остаётся строкой.
    """
    rows = db.query(f"""
    WITH d AS (SELECT max(captured_at) dt FROM supplier_stock),
    st AS (
        SELECT left(ss.external_code, 4) AS code,
               sum(ss.stock) FILTER (WHERE ss.store IN ('Звездный','Дисквер'))                    AS qty_own,
               sum(ss.stock) FILTER (WHERE ss.store = 'Удаленный склад'
                                     AND ss.stock >= {MIN_SUP_STOCK})                             AS qty_remote,
               sum(ss.cost_seb * ss.stock) FILTER
                   (WHERE ss.store IN ('Звездный','Дисквер') AND ss.cost_seb > 0)                 AS seb_sum,
               sum(ss.stock) FILTER
                   (WHERE ss.store IN ('Звездный','Дисквер') AND ss.cost_seb > 0)                 AS seb_qty,
               min(ss.buy_price) FILTER (WHERE ss.store = 'Удаленный склад' AND ss.buy_price > 0
                                          AND ss.stock >= {MIN_SUP_STOCK})                        AS ms_remote
        FROM supplier_stock ss, d
        WHERE ss.captured_at = d.dt AND coalesce(ss.external_code, '') <> ''
        GROUP BY 1
    ),
    ll AS (SELECT DISTINCT ON (supplier_key) id FROM prc_price_load
           WHERE status = 'ok' ORDER BY supplier_key, moment DESC),
    sup AS (
        SELECT left(p.external_code, 4) AS code, min(r.price_rub) AS price_min
        FROM ll JOIN prc_price_row r ON r.load_id = ll.id AND r.status = 'loaded' AND r.price_rub > 0
                                     AND coalesce(r.qty, 0) >= {MIN_SUP_STOCK}
                JOIN products p      ON p.ms_id = r.ms_id
        WHERE coalesce(p.external_code, '') <> '' GROUP BY 1
    ),
    tc AS (
        SELECT left(external_code, 4) AS code, min(buy_price) AS price_min
        FROM tc_buy_price_latest WHERE buy_price > 0 GROUP BY 1
    ),
    tk AS (
        SELECT left(external_code, 4) AS code, min(buy_price) AS price_min
        FROM tc_buy_price_last_known WHERE buy_price > 0 GROUP BY 1
    ),
    codes AS (
        SELECT code FROM st UNION SELECT code FROM sup
        UNION SELECT code FROM tc UNION SELECT code FROM tk
    )
    -- ВАЖНО: supplier_min — только РЕАЛЬНЫЕ предложения поставщиков (остатки на Удаленном
    -- складе и прайс-листы). Цены из tc_* сюда НЕ входят: это НАША прошлая закупка, а не то,
    -- по чему мы купим сегодня. Смешивание стоило ошибки 11.09.2026: по 0025 ТК дал 840 ₽
    -- (одна купленная штука), и бандл 0025X6 считался по 840×6, хотя докупать шесть штук
    -- пришлось бы у Колортека по 981 ₽. Цена ТК возвращается отдельным полем tc_price —
    -- она нужна наборам как собственная закупочная цена комплекта.
    SELECT c.code, st.qty_own, st.qty_remote, st.seb_sum, st.seb_qty,
           LEAST(sup.price_min, st.ms_remote)   AS supplier_min,
           coalesce(tc.price_min, tk.price_min) AS tc_price
    FROM codes c
    LEFT JOIN st  ON st.code  = c.code
    LEFT JOIN sup ON sup.code = c.code
    LEFT JOIN tc  ON tc.code  = c.code
    LEFT JOIN tk  ON tk.code  = c.code
    """)
    out = {}
    for r in rows:
        seb_qty = float(r["seb_qty"] or 0)
        out[r["code"]] = {
            "qty_own": float(r["qty_own"] or 0),
            "qty_remote": float(r["qty_remote"] or 0),
            "cost_own": (float(r["seb_sum"]) / seb_qty) if seb_qty > 0 and r["seb_sum"] else None,
            "supplier_min": float(r["supplier_min"]) if r["supplier_min"] else None,
            "tc_price": float(r["tc_price"]) if r["tc_price"] else None,
        }
    return out


def unit_cost(code, links, stock, need=1):
    """Себестоимость одной позиции. → (цена, источник, ветка).

    Кандидаты = сам код плюс все связанные (универсальные модели — тот же товар другой
    моделью, отгрузить можно любым). Порядок веток задан правилом Сергея:
      есть остаток на Звездном/Дисквере → себестоимость закупленного;
      остаток только на Удаленном       → минимальная цена поставщика;
      остатка нет нигде                 → товар не в продаже (ветка 'none').

    need — сколько штук нужно на одну продажу (кратность бандла). Свой склад берётся ТОЛЬКО
    когда его хватает на всю потребность: правило Сергея от 11.09.2026. У 0025 на складе
    1 шт по 818 ₽, а бандл 0025X6 — это шесть штук; закажут бандл — недостающее поедет
    у Колортека по 1000 ₽, значит и считать надо от Колортека, иначе пол занижен.
    """
    cand = {code} | set(links.get(code) or ())
    own = [stock[c] for c in cand if stock.get(c) and stock[c]["qty_own"] > 0 and stock[c]["cost_own"]]
    if own and sum(x["qty_own"] for x in own) < need:
        own = []                                     # своего не хватает на комплект
    # Правило Сергея 17.09.2026: свой себест ниже половины рыночной цены — это не себестоимость.
    # Так в МС приходит замена брака от поставщика: товар оприходован за 1 ₽. У 3641 такой
    # рубль дал пол 902 ₽ вместо 15 521 ₽ — товар прошёл бы в акцию на любой скидке.
    # Считаем по рынку: докупать-то придётся у поставщика. Если рынка нет вовсе (0334 —
    # остатка у поставщиков нет), ветка уходит дальше и товар остаётся без себестоимости.
    if own:
        mkt = [x for x in (min([p for p in (stock[c].get("supplier_min"), stock[c].get("tc_price"))
                                if p] or [0]) for c in cand if stock.get(c)) if x]
        if mkt and (sum(x["cost_own"] * x["qty_own"] for x in own) / sum(x["qty_own"] for x in own)
                    < OWN_COST_MIN_SHARE * min(mkt)):
            own = []
    if own:
        # несколько связанных кодов с остатком — средневзвешенная по количеству
        qty = sum(x["qty_own"] for x in own)
        return sum(x["cost_own"] * x["qty_own"] for x in own) / qty, "own_stock", "own"
    has_remote = any(stock.get(c) and stock[c]["qty_remote"] > 0 for c in cand)
    prices = [stock[c]["supplier_min"] for c in cand if stock.get(c) and stock[c]["supplier_min"]]
    if has_remote:
        if prices:
            return min(prices), "supplier_min", "remote"
        # предложения поставщика нет — остаётся наша прошлая закупка из ТК. Хуже, чем живая
        # цена, поэтому источник называется явно и виден в отчёте.
        tcs = [stock[c]["tc_price"] for c in cand if stock.get(c) and stock[c]["tc_price"]]
        return (min(tcs) if tcs else None), ("tc_last_buy" if tcs else None), "remote"
    return None, None, "none"


def cogs_map(acc, goods=None):
    """{nm_id: dict(cogs, source, branch, external_code)} — себестоимость по правилам Сергея.

    Порядок: нормализация производного кода → набор (наличие по компонентам, цена своя) →
    добор связанных кодов → себестоимость по месту остатка.
    """
    sets_, links, stock = sets_map(), links_map(), stock_map()
    out = {}
    # max(captured_date) берётся ПО СВОЕМУ КАБИНЕТУ. Глобальный максимум по всей таблице —
    # дефект, стоивший волны h8 11.09.2026: срез за 11-е к 08:00 МСК успел собраться по
    # wb_acc2, но ещё не по wb_acc1, максимум сдвинулся на новую дату — и запрос по acc1
    # вернул НОЛЬ строк. Кабинет ослеп целиком: себестоимость известна 0 из 16 085, и 195
    # карточек уехали «возвратом к доакционной цене» просто потому, что считать пол было не из чего.
    rows = db.query("""SELECT nm_id, vendor_code, external_code FROM mkt_margin_control
                       WHERE account = %s
                         AND captured_date = (SELECT max(captured_date) FROM mkt_margin_control
                                              WHERE account = %s)""",
                    (acc, acc))
    # Справочник покрывает не весь каталог: 11.09.2026 в нём 13 255 карточек из 20 573 по acc1
    # и 10 641 из 17 270 по acc2. Карточка без строки уходила из акции с причиной «нет
    # себестоимости», хотя её артикул — обычный 4-значный код, по которому себестоимость
    # прекрасно считается: по acc2 таких было 896, у 815 код живой, 104 прошли бы пол и уехали
    # из акции зря. Добираем недостающие nm_id прямо из выдачи ВБ — внешнего кода у них нет,
    # его роль играет артикул продавца, ровно тот же путь, что у бандлов ниже.
    if goods:
        seen = {int(r["nm_id"]) for r in rows}
        rows = list(rows) + [{"nm_id": nm, "vendor_code": g[3], "external_code": None}
                             for nm, g in goods.items() if int(nm) not in seen and g[3]]
    for r in rows:
        # Внешнего кода может не быть — тогда пробуем артикул площадки: у части карточек
        # (все бандлы acc1) артикул продавца И ЕСТЬ внешний код, просто привязка в справочнике
        # не проставлена. Без этого 4248X10 и 0025X6 висели как 'unmapped' при живых остатках.
        raw = (r["external_code"] or "").strip()
        from_vc = not raw
        if from_vc:
            # Артикул в роли внешнего кода. Проверено на acc1 11.09.2026: там, где заполнены оба
            # поля, первые 4 знака совпадают у 96,5 % карточек (9497 из 9837). Из 3491 карточки
            # без внешнего кода 3436 — бандлы вида 0000X00, то есть ровно тот класс, для которого
            # артикул и есть код с кратностью. Остальные 55 — обычные коды.
            raw = (r["vendor_code"] or "").strip()

        # БАНДЛ: 4248X10 — это 10 штук товара 4248 в одной упаковке, 0025X6 — шесть штук 0025.
        # Себестоимость карточки = себестоимость единицы × кратность. Без множителя пол
        # считался бы по одной штуке и был бы занижен в 6-10 раз. Шаблон якорный
        # (ровно «4 цифры + X + число»), чтобы не зацепить обычные производные коды.
        mult, m = 1, re.match(r"^(\d{4})[Xх](\d{1,2})$", raw, re.I)
        if m:
            mult = int(m.group(2))

        code = norm_code(raw)
        # 'unmapped' — карточка ВБ вообще не связана с номенклатурой МойСклада (у acc1 таких
        # 3443). Действие то же, что при отсутствии себестоимости, но причина другая.
        rec = {"cogs": None, "source": None, "branch": ("none" if code else "unmapped"),
               "external_code": raw or None, "vendor_code": r["vendor_code"],
               "n_comp": 0, "bundle": mult, "code_from": ("vendor_code" if from_vc else "external_code")}
        if code:
            self_rec = stock.get(code) or {}
            comps = sets_.get(code) or {code: 1}
            rec["n_comp"] = len(comps)

            if len(comps) == 1:                          # обычный товар, не набор
                price, src, branch = unit_cost(code, links, stock, need=mult)
                rec.update(cogs=price, source=src, branch=branch)
                if mult > 1 and price:
                    rec["cogs"], rec["source"] = float(price) * mult, f"{src}_x{mult}"
                out[int(r["nm_id"])] = rec
                continue

            # НАБОР. Комплект на складе отдельной единицей обычно не лежит — он собирается
            # из компонентов, поэтому НАЛИЧИЕ считаем по компонентам. А ЦЕНУ берём собственную,
            # если она известна: у 633 наборов из 931 в ТК есть закупочная цена комплекта,
            # и у 385 из них она ВЫШЕ суммы компонентов, иногда впятеро (6664: 5899 против 597).
            # Комплект закупается как комплект — его цена и есть себестоимость. Сумма по
            # компонентам остаётся фолбэком, когда своей цены нет (таких наборов 33).
            # Считать наборы только по компонентам — значит занизить пол и продавать в минус:
            # 6528 — 19 353 ₽ своя цена против 8 874 ₽ по частям, пол расходится вдвое.
            if self_rec.get("qty_own", 0) >= mult and self_rec.get("cost_own"):
                rec.update(cogs=self_rec["cost_own"], source="set_own_stock", branch="own")
                if mult > 1:
                    rec["cogs"], rec["source"] = float(rec["cogs"]) * mult, f"set_own_stock_x{mult}"
                out[int(r["nm_id"])] = rec
                continue

            total, srcs, branches, gap = 0.0, set(), set(), False
            for comp, qty in comps.items():
                price, src, branch = unit_cost(comp, links, stock, need=qty * mult)
                branches.add(branch)
                if price is None:
                    gap = True
                else:
                    total += price * qty
                    srcs.add(src)
            if "none" in branches:                       # хоть один компонент не в продаже
                rec["branch"] = "none"                   # — набор не собрать
            else:
                rec["branch"] = "own" if "own" in branches else "remote"
                own_price = self_rec.get("tc_price") or self_rec.get("supplier_min")
                if own_price:
                    rec.update(cogs=float(own_price), source="set_own_price")
                elif not gap:
                    rec.update(cogs=total, source="set_components")
        if mult > 1 and rec.get("cogs"):
            rec["cogs"] = float(rec["cogs"]) * mult
            rec["source"] = (rec["source"] or "") + f"_x{mult}"
        out[int(r["nm_id"])] = rec
    return out


def prepromo_map(acc, cycle_start):
    """{nm_id: доакционная цена}: сначала из журнала (базлайн), потом из снимка wb_price.

    Порядок важен. Базлайн снимается накануне старта (`--baseline`) и лежит в журнале ДО
    cycle_start, поэтому окно поиска смотрит назад на BASELINE_WINDOW дней; берём самую раннюю
    запись цикла. Снимок wb_price годится только если он сделан до старта акции: коллектор
    в 09:20 перезаписывает его акционными ценами, и тогда «доакционная» цена окажется дном
    акции, а вывод товара — пустым.
    """
    out = {}
    since = min(cycle_start, datetime.now(MSK)) - timedelta(days=BASELINE_WINDOW)
    # Источник — ТОЛЬКО строки wave='baseline' и самая свежая из них. Брать «самую раннюю
    # запись цикла» нельзя: строки волн тоже несут prepromo_price, и на h2/h8 сторож сравнивал
    # бы цену сам с собой. Свежий базлайн важен и по другой причине: цены каталога двигает
    # заливка ТК (price_quarantine, 4 раза в сутки) — со старым снимком наше же законное
    # снижение цены выглядело бы как работа акции, и сторож откатывал бы его назад.
    for r in db.query("""SELECT DISTINCT ON (nm_id) nm_id, prepromo_price
                         FROM wb_promo_guard_log
                         WHERE account = %s AND wave = 'baseline' AND ts >= %s
                           AND prepromo_price IS NOT NULL
                         ORDER BY nm_id, ts DESC""", (acc, since)):
        out[int(r["nm_id"])] = float(r["prepromo_price"])
    for r in db.query("""SELECT nm_id, discounted_price, price FROM wb_price
                         WHERE account = %s AND captured_at < %s""", (acc, cycle_start)):
        nm = int(r["nm_id"])
        if nm in out:
            continue
        v = r["discounted_price"] or r["price"]
        if v:
            out[nm] = float(v)
    return out


def raise_plan(base, target):
    """Как поднять цену покупателя до target. → (price, discount) для upload/task.

    Поднимаем ЗА СЧЁТ СКИДКИ: база остаётся своей, режется скидка. Правило Сергея
    от 10.09.2026. Прежняя механика слала price=target, discount=0 — база при этом
    оставалась старой, скидка обнулялась, и цена покупателя улетала не к цели, а к базе:
    3360SI3A354N ушёл на 97 672 вместо 60 224, 6623H9218HN1 на 8 140 вместо 3 756.
    Базу трогаем только когда её самой не хватает: base < target даже при нулевой скидке.
    floor() на скидке — округление всегда в сторону цены выше цели, ниже пола не падаем.
    """
    base, target = float(base or 0), float(target)
    if base >= target:
        return int(math.ceil(base)), int(math.floor(100.0 * (1.0 - target / base)))
    return int(math.ceil(target)), 0


def mp_stock(acc):
    """{nm_id: остаток на ВБ} = склад продавца (FBS, mp) + склад ВБ (FBO, wb).

    Правило Сергея от 11.09.2026 про бандлы. Для упаковки (`4248X10` = 10 × `4248`) сумма
    штук на складе о доступности не говорит: комплектность считает ТК, он же передаёт на ВБ
    остаток по карточке бандла. Передаём остаток — считаем себестоимость и участвуем в акции;
    не передаём — выводим. Прежний критерий «штук на складе >= кратности» давал ложные
    срабатывания: по нему 197 бандлов acc1 участвовали в акции, хотя остатка по ним на ВБ нет.

    ПОЧЕМУ ИМЕННО ВОРОНКА. `/api/v3/stocks` — нужен скоуп «Маркетплейс», его нет ни у одного
    токена; `calendar/promotions/nomenclatures` на type=auto отвечает 422; `wb_stocks` — только
    FBO-склады ВБ, карточек бандлов там нет вовсе. Воронка продаж отдаёт `stocks.mp` по каждой
    карточке под скоупом «Аналитика», который у нас есть. ~13 запросов на кабинет, ~1,5 мин.
    """
    tok = os.getenv(FUNNEL_TOKEN_ENV[acc])
    if not tok:
        print(f"[{acc}] {FUNNEL_TOKEN_ENV[acc]} не задан — остаток на ВБ не проверяем", flush=True)
        return {}
    # Окно 30 дней, а не «сегодня»: воронка отдаёт только карточки с активностью за период,
    # и на однодневном окне их 12 936 из 20 573, а на месячном — 13 529, причём нулевых
    # остатков видно 609 против 15. Остаток в ответе текущий, от длины окна не зависит.
    end = datetime.now(MSK).date()
    start = (end - timedelta(days=30)).isoformat()
    end = end.isoformat()
    hdr = {"Authorization": tok, "Content-Type": "application/json"}
    out, off = {}, 0
    while True:
        body = {"nmIDs": [], "brandNames": [], "subjectIDs": [], "tagIDs": [],
                "selectedPeriod": {"start": start, "end": end},
                "orderBy": {"field": "openCard", "mode": "desc"}, "limit": PAGE, "offset": off}
        try:
            r = requests.post(FUNNEL_URL, headers=hdr, json=body, timeout=120)
        except Exception as e:
            print(f"[{acc}] воронка: сбой запроса {e}", flush=True)
            return {}
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 2)
            continue
        if r.status_code != 200:
            print(f"[{acc}] воронка: HTTP {r.status_code}", flush=True)
            return {}
        prods = (r.json().get("data") or {}).get("products") or []
        for p in prods:
            pr = p.get("product") or {}
            if pr.get("nmId"):
                st = pr.get("stocks") or {}
                out[pr["nmId"]] = (st.get("mp") or 0) + (st.get("wb") or 0)
        if len(prods) < PAGE:
            break
        off += PAGE
        time.sleep(3)
    return out


def decide(acc, goods, cogs, prepromo, ret, touched_only=True):
    """Решение по каждому nm_id. → список словарей-строк журнала.

    touched_only=True (по умолчанию и в кроне) — трогаем ТОЛЬКО те карточки, которым цену
    уронила акция: текущая цена ниже доакционной. Без этого ограничения сторож начинает
    поднимать цену всему, что стоит ниже пола (10.09.2026 — 3367 карточек), а это уже не
    «вывести из акции», а переоценка каталога. Снять ограничение можно только руками: --all.
    """
    rows = []
    for nm, (base, disc, cur, vendor) in goods.items():
        if not cur or cur <= 0:
            continue
        c = cogs.get(nm) or {}
        pre = prepromo.get(nm)
        touched = bool(pre) and cur < pre * DROP_EPS
        row = {"account": acc, "nm_id": nm, "vendor_code": c.get("vendor_code") or vendor,
               "external_code": c.get("external_code"), "price_before": base, "disc_before": disc,
               "buyer_before": cur, "prepromo_price": pre, "cogs": c.get("cogs"),
               "cogs_source": c.get("source"), "stock_branch": c.get("branch"),
               "retention": ret, "floor_net": None, "floor_price": None, "net_before": None,
               "target_price": None, "send_price": None, "send_disc": None,
               "status": "skip", "reason": "", "err": None, "price_after": None}

        if touched_only and not touched:
            row["reason"] = ("акция цену не трогала" if pre else "нет доакционного снимка")
            rows.append(row)
            continue

        if c.get("cogs"):
            cg = float(c["cogs"])
            floor_net = max(MIN_NET, MIN_NET_PCT * cg)
            floor_price = (floor_net + cg) / (1.0 - ret)
            row["floor_net"] = round(floor_net, 2)
            row["floor_price"] = round(floor_price, 2)
            row["net_before"] = round(cur * (1.0 - ret) - cg, 2)
            if cur >= floor_price - 0.5:
                row["reason"] = "проходит по полу"
                rows.append(row)
                continue
            target = float(math.ceil(floor_price))
            if target <= cur:                        # цену только поднимаем, вниз не ходим
                row["reason"] = "пол ниже текущей цены"
                rows.append(row)
                continue
            row["target_price"] = target
            row["send_price"], row["send_disc"] = raise_plan(base, target)
            row["status"] = "todo"
            row["reason"] = f"ниже пола на {floor_price - cur:.0f} ₽, чистая {row['net_before']:.0f} ₽"
        else:
            # Себестоимости нет — считать пол не из чего. Выводим из акции возвратом
            # к доакционной цене, но только если цена реально просела: иначе снесли бы
            # обычную рабочую скидку карточки, которая к акции отношения не имеет.
            # Ветка 'none' — остатка нет ни у нас, ни у поставщика (для набора — хотя бы
            # у одного компонента). Такой товар не в продаже, его в акции быть не должно
            # независимо от пола: правило Сергея от 10.09.2026.
            why = {"none": "товар не в продаже (нет остатка нигде)",
                   "no_mp_stock": "товар не в продаже (остаток на ВБ не передаётся)",
                   "unmapped": "нет привязки к МойСкладу"}.get(c.get("branch"), "нет себестоимости")
            if touched:
                row["target_price"] = float(math.ceil(pre))
                row["send_price"], row["send_disc"] = raise_plan(base, math.ceil(pre))
                row["status"] = "todo"
                row["reason"] = why + ", возврат к доакционной цене"
            else:
                row["reason"] = why + ", цена не просела"
        rows.append(row)
    return rows


def push(acc, batch):
    """Отправить пачку цен одной задачей. → (ok, текст ошибки).

    Цена и скидка берутся из raise_plan(): база сохраняется, поднимаем срезом скидки.
    """
    body = {"data": [{"nmID": int(r["nm_id"]),
                      "price": int(r["send_price"] if r.get("send_price") else math.ceil(r["target_price"])),
                      "discount": int(r.get("send_disc") or 0)}
                     for r in batch]}
    r = None
    for attempt in range(4):
        try:
            r = requests.post(WB_API + "/api/v2/upload/task", headers=head(acc), json=body, timeout=90)
        except requests.RequestException as e:
            if attempt == 3:
                return False, f"сеть: {str(e)[:150]}"
            time.sleep(2 ** attempt * 3)
            continue
        if r.status_code < 500:
            break
        if attempt == 3:
            return False, f"HTTP {r.status_code} {r.text[:200]}"
        time.sleep(2 ** attempt * 3)
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code} {r.text[:200]}"
    uid = (r.json().get("data") or {}).get("id")
    if not uid:
        return False, f"нет uploadID: {r.text[:200]}"
    for _ in range(15):                              # задача обрабатывается асинхронно
        time.sleep(4)
        try:
            d = wb_get(acc, WB_API + "/api/v2/history/tasks", {"uploadID": uid}).get("data") or {}
        except Exception:
            continue
        if isinstance(d, list):
            d = d[0] if d else {}
        # Пустой ответ = задача ещё не заведена в истории, а не «провал»: сразу после
        # /upload/task ВБ отдаёт data без полей, и status приходит None. До 15.09.2026 такой
        # ответ считался терминальным, из-за чего волна 00:30 записала 100 ложных ошибок —
        # цены при этом действительно не применились, задача просто не была опрошена до конца.
        if not d or d.get("status") in (1, 2):
            continue
        if (d.get("successGoodsNumber") or 0) > 0:
            return True, ""
        return False, f"status={d.get('status')} {task_err(acc, uid)}".strip()
    return False, "вердикта задачи не дождались"


def task_err(acc, uid):
    try:
        it = ((wb_get(acc, WB_API + "/api/v2/history/goods/task",
                      {"uploadID": uid, "limit": 10}).get("data") or {}).get("historyGoods") or [])
        return (it[0].get("errorText") or "") if it else ""
    except Exception:
        return ""


def save(rows):
    if not rows:
        return
    cols = ["account", "nm_id", "vendor_code", "external_code", "wave", "promo_ids",
            "price_before", "disc_before", "buyer_before", "prepromo_price", "cogs", "cogs_source",
            "stock_branch", "retention", "floor_net", "floor_price", "net_before", "target_price",
            "status", "reason", "err", "price_after"]
    sql = f"INSERT INTO wb_promo_guard_log ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])


def tg(text):
    if not TG_TOKEN:
        return "нет TG_PRC_BOT_TOKEN"
    for cid in NOTIFY_IDS:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": cid, "text": text, "disable_web_page_preview": True},
                          timeout=30)
        except Exception as e:
            return str(e)[:120]
    return "ok"


def baseline(accounts):
    """Снять доакционный снимок цен в журнал.

    Единственный надёжный способ отличить «цену уронила акция» от «карточка всегда так
    стоит»: витрина wb_price к моменту проверки уже перезаписана акционными ценами
    (коллектор в 09:20 МСК), а из API ВБ состав автоакции не отдаёт — `nomenclatures`
    на type=auto отвечает 422. Поэтому снимок делаем сами, накануне старта.
    """
    total = 0
    for acc in accounts:
        goods = fetch_goods(acc)
        rows = [{"account": acc, "nm_id": nm, "vendor_code": v, "external_code": None,
                 "wave": "baseline", "promo_ids": None, "price_before": base, "disc_before": disc,
                 "buyer_before": cur, "prepromo_price": cur, "cogs": None, "cogs_source": None,
                 "stock_branch": None, "retention": None, "floor_net": None, "floor_price": None,
                 "net_before": None, "target_price": None, "status": "baseline",
                 "reason": "доакционный снимок", "err": None, "price_after": None}
                for nm, (base, disc, cur, v) in goods.items() if cur and cur > 0]
        save(rows)
        total += len(rows)
        print(f"[{acc} {ACC_NAME[acc]}] базлайн: {len(rows)} карточек", flush=True)
    print(f"Доакционный снимок снят: {total} карточек", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="боевой прогон: писать цены в ВБ")
    ap.add_argument("--dry", action="store_true", help="только посчитать и показать (по умолчанию)")
    ap.add_argument("--acc", default=None, help="wb_acc1 | wb_acc2 (по умолчанию оба)")
    ap.add_argument("--wave", default="manual", help="метка волны: start | h2 | h8 | manual")
    ap.add_argument("--limit", type=int, default=0, help="править не более N товаров (страховка)")
    ap.add_argument("--baseline", action="store_true",
                    help="снять доакционный снимок цен (запускать НАКАНУНЕ старта акции)")
    ap.add_argument("--neg-only", action="store_true", dest="neg_only",
                    help="править только карточки с отрицательной чистой (для разбора идущих акций)")
    ap.add_argument("--all", action="store_true",
                    help="снять защиту: править ВСЁ, что ниже пола, а не только уронённое акцией")
    ap.add_argument("--no-tg", action="store_true")
    a = ap.parse_args()
    apply = a.apply and not a.dry

    accounts = [a.acc] if a.acc else ["wb_acc1", "wb_acc2"]

    if a.baseline:
        return baseline(accounts)
    stamp = datetime.now(MSK).strftime("%Y-%m-%d_%H%M")
    all_rows, summary, alarms = [], [], []

    for acc in accounts:
        ret = RETENTION[acc]
        promos, promo_start = active_promos(acc)
        # Цикл = текущая акция. Нет активной акции (ручной прогон) — берём сегодняшнюю полночь МСК.
        cycle_start = promo_start or datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
        goods = fetch_goods(acc)
        cogs = cogs_map(acc, goods)
        # Доступность решает остаток, который мы САМИ передаём на ВБ, а не штуки на складе
        # поставщика. Правило Сергея от 12.09.2026 на примере 5801/5368: на Удаленном складе
        # они есть, но поставщик временно не отгружает — остаток обнулили, продавать нечего.
        # Сначала правило ввели только для бандлов (там штуки считает ТК), теперь оно общее:
        # источник истины один и тот же для любой карточки.
        # ВАЖНО: воронка возвращает НЕ весь каталог (acc1: 12 967 из 20 573) — карточки без
        # активности в неё не попадают. Поэтому «нет в выдаче» ≠ «нет остатка»: выводим только
        # тех, кто В ВЫДАЧЕ ЕСТЬ и остаток по ним нулевой. Иначе снесли бы полкаталога.
        mps = mp_stock(acc)
        if len(mps) >= MP_MIN_CARDS:
            n_out = 0
            for nm, c in cogs.items():
                if nm in mps and not mps[nm]:
                    c["cogs"], c["source"], c["branch"] = None, None, "no_mp_stock"
                    n_out += 1
            print(f"[{acc}] остаток на ВБ: в воронке {len(mps)} карточек, "
                  f"нулевых из наших {n_out} — выводим из акции", flush=True)
        else:
            msg = (f"[{acc}] ВОРОНКА НЕ ОТВЕТИЛА: карточек {len(mps)} (порог {MP_MIN_CARDS}) — "
                   f"остаток на ВБ не проверен, считаем по складским остаткам")
            print(msg)
            alarms.append(msg)
        known = sum(1 for c in cogs.values() if c.get("cogs"))
        if known < COGS_MIN_KNOWN:
            # Ослепший справочник не должен превращаться в массовый вывод из акций: без
            # себестоимости каждая просевшая карточка уходит в ветку «возврат к доакционной
            # цене». Лучше пропустить кабинет и позвать человека, чем снести акции вслепую.
            msg = (f"[{acc}] СПРАВОЧНИК НЕ ГОТОВ: себестоимость известна {known} "
                   f"(порог {COGS_MIN_KNOWN}) — кабинет пропущен, цены не трогаем")
            print(msg)
            alarms.append(msg)
            continue
        pre = prepromo_map(acc, cycle_start)
        rows = decide(acc, goods, cogs, pre, ret, touched_only=not a.all)
        pid = ",".join(str(p[0]) for p in promos)
        for r in rows:
            r["wave"], r["promo_ids"] = a.wave, pid

        if a.neg_only:
            # Разбор УЖЕ идущих акций: базлайна по ним нет, поэтому «уронила ли акция» не
            # проверить. Сужаем до безусловного признака — продаём в убыток (чистая < 0).
            for r in rows:
                if r["status"] == "todo" and not ((r["net_before"] or 0) < 0):
                    r["status"], r["reason"] = "skip", r["reason"] + " (не минус, --neg-only)"
        todo = sorted([r for r in rows if r["status"] == "todo"],
                      key=lambda r: -(r["floor_price"] or 0) + (r["buyer_before"] or 0))
        if a.limit:
            for r in todo[a.limit:]:
                r["status"], r["reason"] = "skip", r["reason"] + " (за пределом --limit)"
            todo = todo[:a.limit]

        loss = [r for r in todo if (r["net_before"] or 0) < 0]
        print(f"[{acc} {ACC_NAME[acc]}] акций активно {len(promos)} ({pid or '—'}), "
              f"карточек {len(goods)}, себест. известна {sum(1 for r in rows if r['cogs'])}, "
              f"под правку {len(todo)} (из них в минусе {len(loss)})", flush=True)

        if apply and todo:
            for i in range(0, len(todo), PUSH_CHUNK):
                chunk = todo[i:i + PUSH_CHUNK]
                ok, err = push(acc, chunk)
                for r in chunk:
                    r["status"] = "sent" if ok else "error"
                    r["err"] = err or None
                print(f"  пачка {i // PUSH_CHUNK + 1}: {len(chunk)} шт — "
                      f"{'отправлена' if ok else 'ОШИБКА ' + err[:80]}", flush=True)
            time.sleep(90)                            # ВБ применяет цены не мгновенно
            after = fetch_goods(acc)                  # подтверждение перечиткой, не «отправили = сделали»
            for r in todo:
                got = after.get(r["nm_id"])
                if got:
                    r["price_after"] = got[2]
                    if r["status"] == "sent" and got[2] >= (r["target_price"] or 0) - 0.5:
                        r["status"] = "confirmed"
        elif todo:
            for r in todo:
                r["status"] = "dry"

        ok_n = sum(1 for r in todo if r["status"] == "confirmed")
        summary.append((acc, len(goods), len(todo), len(loss), ok_n,
                        sum(1 for r in todo if r["status"] == "error")))
        all_rows += rows

    save(all_rows)

    # Полный разбор — файлом; в чат и в ТГ идёт только сводка (правило экономии контекста).
    path = f"{REPORTS}/wb_promo_guard_{stamp}.csv"
    changed = [r for r in all_rows if r["status"] in ("todo", "dry", "sent", "confirmed", "error")]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["account", "nm_id", "vendor_code", "external_code", "cogs", "cogs_source",
                    "stock_branch", "buyer_before", "floor_price", "net_before", "target_price",
                    "price_after", "status", "reason", "err"])
        for r in sorted(changed, key=lambda r: (r["account"], r["net_before"] or 0)):
            w.writerow([r["account"], r["nm_id"], r["vendor_code"], r["external_code"],
                        r["cogs"] and round(r["cogs"], 2), r["cogs_source"], r["stock_branch"],
                        r["buyer_before"], r["floor_price"], r["net_before"], r["target_price"],
                        r["price_after"], r["status"], r["reason"], r["err"]])

    lines = [f"Сторож автоакций WB — волна {a.wave}, {'БОЕВОЙ' if apply else 'сухой'} прогон"]
    lines += alarms
    for acc, ng, nt, nl, nok, ne in summary:
        lines.append(f"{ACC_NAME[acc]}: карточек {ng}, под правку {nt} (в минусе {nl}), "
                     f"подтверждено {nok}, ошибок {ne}")
    lines.append(f"Отчёт: {path}")
    text = "\n".join(lines)
    print(text, flush=True)
    if apply and not a.no_tg:
        tg(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

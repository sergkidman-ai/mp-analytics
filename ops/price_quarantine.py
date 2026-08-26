#!/usr/bin/env python3
# поток: mkt
"""Карантин цен на площадках → проверка «не уйдём ли в минус» перед разблокировкой.

Цены на карточки шлёт ТК несколько раз в день; при изменении в разы площадка сажает товар
в карантин цен (WB — цена со скидкой втрое ниже прежней, Маркет — вердикты PRICE_CHANGE /
LOW_PRICE, Ozon — свыше ~50 %). Пока товар в карантине, он не продаётся по новой цене.

Скрипт читает карантин по API, подставляет себестоимость и считает, остаётся ли после
удержаний площадки прибыль выше пола. Без `--apply` на площадки ничего не пишет.

Выпуск (`--apply`) по вердикту ОК:
  * Маркет — штатный метод price-quarantine/confirm (пачками до 200 offerId);
  * WB — метода выпуска у API нет: карантин снимается ЛЕСТНИЦЕЙ — цена опускается шагами,
    каждый из которых меньше порога площадки, до целевой цены ТК. Проверено 23.08.2026 на
    nmID 209727462: перезалив ТОЙ ЖЕ цены отбивается («New price is several times lower…»),
    а три шага 12906 → 10970 → 9324 → 8363 прошли и вывели карточку из карантина.
    Шаг адаптивный: пробуем 25 %, на отказе повторяем 15 % (−15 % проверено практикой).

Правила (решения Сергея 23.08.2026):
  * удержания площадки — ОДНИМ числом из витрин «Отчёты МП» за последний закрытый месяц;
  * себест — ЖИВАЯ ЗАКУПКА ТК (`/api/catalog/best`), потому что именно от неё платформа
    считает цену продажи: сверять её цену с другой базой — сравнивать разные величины
    (решение Сергея 23.08.2026, случай кода 3060: у ТК 113 ₽, средневзвешенная по остатку
    МС 1978 ₽ — семь поставщиков от 105 до 2844 ₽, цена ТК посчитана от дешёвой строки);
  * производные карточки («15301», «153010» и прочие 1530***) — тот же товар, что базовый
    4-значный код: себест берём равным себесту 1530 (решение Сергея 23.08.2026);
  * карточка-набор (3241 = 3237+3238+3239+3240) — себест равен СУММЕ живых закупок ТК
    по каждому компоненту из справочника наборов; неполный состав не считаем вовсе;
  * нет цены у ТК → фолбэк на остаток МойСклада: Звездный → Цифровой, Дисквер → Дисквэр,
    Удаленный склад → общий;
  * нет ни цены ТК, ни остатка → товар остаётся в карантине;
  * пол прибыли = 10 % от цены продажи. Абсолютные 300 ₽ отменены Сергеем 23.08.2026: они
    связывали только дешёвый товар (10 % от 3000 ₽ и есть 300 ₽), и такие карточки навсегда
    оставались бы в карантине — товар за 460 ₽ при удержаниях 55 % трёхсот рублей не даст.

Запуск:  ./venv/bin/python ops/price_quarantine.py [--apply]
"""
import os
import re
import sys
import csv
import json
import pathlib
import datetime
import time
import argparse
import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                               # noqa: E402
from collectors.wb_prices import _token as wb_token               # noqa: E402

WB_API = "https://discounts-prices-api.wildberries.ru"
YA_API = "https://api.partner.market.yandex.ru"
TC_API = "https://thecartridge.ru/api/catalog/best"
TC_BATCH = 100                    # жёсткий потолок платформы: 101 код → HTTP 422
MIX_API = "https://thecartridge.ru/api/catalog/mix_data"   # состав набора
STORE_OF_ACC = {"acc1": "Звездный", "acc2": "Дисквер"}
COMMON_STORE = "Удаленный склад"
FLOOR_PCT = 0.10                  # пол прибыли = 10 % цены продажи (абсолютный пол отменён)
YA_BANDS = (1000, 3000, 10000, 25000)   # границы диапазонов цены заказа Маркета, ₽
YA_BAND_MIN_N = 30                # тоньше — своей ставке диапазона не верим
WB_STEPS = (0.20, 0.15, 0.10)     # шаги лестницы WB: крупный → на отказе мельче (−25 % отбит,
WB_MAX_STEPS = 15                 #                    −15 % проверен практикой 23.08.2026)
WB_PACE = 0.8                     # лимит WB — 10 запросов / 6 с на эндпоинт
HIST = BASE_DIR / "reports" / "data"


# ─── удержания площадки: одно число из витрин «Отчёты МП» ────────────────────────────────
def _last_closed(d):
    """Индекс последнего месяца, который уже сверен (не provisional)."""
    prov = set(d.get("provisional") or [])
    keys = d.get("period_keys") or []
    for i in range(len(d["months"]) - 1, -1, -1):
        if not keys or keys[i] not in prov:
            return i
    return len(d["months"]) - 1


def retention():
    """{account: доля удержаний площадки от оборота} — как в строке «Итого удержания»."""
    out, month = {}, None
    d = json.loads((HIST / "mp_ozon_hist.json").read_text())
    i = _last_closed(d); month = d["months"][i]
    exp = ["commission", "delivery", "partners", "fbo", "promo", "penalty", "unclassified"]
    for acc, a in d["accounts"].items():
        L = a["lines"]
        out[acc] = sum(L[k][i] for k in exp) / L["sales"][i]

    d = json.loads((HIST / "mp_wb_hist.json").read_text())
    i = _last_closed(d)
    exp = ["delivery", "storage", "acceptance", "ads", "points", "penalty", "other"]
    for acc, a in d["accounts"].items():
        L = a["lines"]; N = len(d["months"])
        ob = L["own_price"][i]
        comp = (L.get("compensation") or [0] * N)[i]
        retp = (L.get("returns_pay") or [0] * N)[i]
        itog = L["to_pay"][i] - sum(L[k][i] for k in exp) + comp
        out[acc] = (ob - itog - retp) / ob

    d = json.loads((HIST / "mp_yandex_hist.json").read_text())
    i = _last_closed(d)
    # promotion — РОДИТЕЛЬСКАЯ строка (бусты/полка/отзывы/реклама уже внутри неё): берём её,
    # компоненты отдельно не складываем, иначе продвижение считается дважды.
    exp = ["fee", "delivery", "transfer", "promotion", "agency", "other_fee", "subscription_cost"]
    for acc, a in d["accounts"].items():
        L = a["lines"]
        out[acc] = sum(L[k][i] for k in exp) / (L["revenue"][i] + L["netting"][i])
    return out, month


def ya_retention_bands():
    """Удержания Маркета ставкой СВОЕГО диапазона цены: [доля на диапазон] по YA_BANDS.

    Одно число по всему обороту («Итого удержания») для дорогого SKU врёт: комиссия
    процентная, а логистика и эквайринг почти фиксированы на заказ, поэтому удержания
    РЕГРЕССИВНЫ — <1к съедают ~60 %, >10к ~32 %. Прикладывать ставку заказа к цене карточки
    законно: 95 % заказов Маркета — одна позиция, 87 % — ровно одна единица (замер 24.08.2026).

    Выручка — `raw_yandex_closure` (только закрытые месяцы), сборы — `raw_yandex_services`;
    сцепка по order_id с отрезанным хвостом «.0» — в services номер сохранён дробным числом,
    в лоб сцепляются 88 заказов из 2055, с отрезанным хвостом — все 2055.

    Тонкому диапазону (n < YA_BAND_MIN_N) своей ставке не верим: берём БОЛЬШУЮ из своей
    и ближайшей плотной — ошибаться безопаснее в сторону завышенных удержаний.
    """
    d = json.loads((HIST / "mp_yandex_hist.json").read_text())
    keys = d.get("period_keys") or []
    last_key = keys[_last_closed(d)] if keys else "9999-12"
    rows = db.query("""
        WITH z AS (SELECT order_id AS o, SUM(amount) AS rev
                     FROM raw_yandex_closure
                    WHERE category = 'revenue' AND ym <= %s
                    GROUP BY 1),
             s AS (SELECT split_part(order_id, '.', 1) AS o, SUM(COALESCE(cost, 0)) AS fee
                     FROM raw_yandex_services
                    GROUP BY 1)
        SELECT z.rev AS rev, s.fee AS fee
          FROM z JOIN s ON s.o = z.o
         WHERE z.rev > 0""", (last_key,))

    agg = [[0, 0.0, 0.0] for _ in range(len(YA_BANDS) + 1)]
    for r in rows:
        rev, fee = float(r["rev"]), float(r["fee"])
        b = agg[ya_band(rev)]
        b[0] += 1
        b[1] += rev
        b[2] += fee

    out, dense = [], None
    for n, rev, fee in agg:
        rate = fee / rev if rev > 0 else None
        if rate is None:
            out.append(dense)                     # данных нет вовсе — ставка плотного соседа
        elif n >= YA_BAND_MIN_N:
            dense = rate
            out.append(rate)
        else:
            out.append(rate if dense is None else max(rate, dense))
    return out


def ya_band(value):
    """Номер диапазона YA_BANDS, в который попадает цена."""
    return next((k for k, hi in enumerate(YA_BANDS) if value < hi), len(YA_BANDS))


def ya_rate(bands, flat, price):
    """Ставка удержаний для цены карточки: своя по диапазону, иначе — плоская по обороту."""
    if not bands:
        return flat
    r = bands[ya_band(price)]
    return flat if r is None else r


def ya_archived(offer_ids):
    """offerId, которые Маркет держит в АРХИВЕ.

    Архивную карточку ЛК в карантине не показывает, а API её всё равно отдаёт: `23542del`
    висел в нашем отчёте «без себеста» вечно, а глазами в интерфейсе его нет (24.08.2026).
    У живого оффера флага в ответе просто НЕТ — истина только `archived is True`.
    """
    if not offer_ids:
        return set()
    key, biz = os.getenv("YANDEX_API_KEY_ACC1"), os.getenv("YANDEX_BUSINESS_ID_ACC1")
    H = {"Api-Key": key, "Content-Type": "application/json"}
    ids, out = list(offer_ids), set()
    for i in range(0, len(ids), 200):
        r = requests.post(f"{YA_API}/businesses/{biz}/offer-mappings", headers=H,
                          json={"offerIds": ids[i:i + 200]}, params={"limit": 200}, timeout=60)
        r.raise_for_status()
        for m in ((r.json().get("result") or {}).get("offerMappings") or []):
            o = m.get("offer") or {}
            if o.get("archived") is True:
                out.add(o.get("offerId"))
    return out


# ─── себестоимость текущего остатка ─────────────────────────────────────────────────────
def cost_map():
    """{external_code: {store: себест/шт}} по свежему срезу остатков МойСклада.

    Внутри склада может лежать товар от разных поставщиков — берём средневзвешенную по штукам.
    """
    rows = db.query("""
        SELECT external_code, store,
               SUM(cost_seb * stock) AS s, SUM(stock) AS q
          FROM supplier_stock
         WHERE captured_at = (SELECT MAX(captured_at) FROM supplier_stock)
           AND stock > 0 AND cost_seb > 0 AND external_code IS NOT NULL
         GROUP BY 1, 2""")
    m = {}
    for r in rows:
        m.setdefault(r["external_code"], {})[r["store"]] = float(r["s"]) / float(r["q"])
    return m


def cost_for(code, acc, cm):
    """Фолбэк-себест из остатка МС: «свой склад → общий склад поставщика»."""
    stores = cm.get(code)
    if not stores:
        return None, None
    own = STORE_OF_ACC["acc2" if acc.endswith("acc2") else "acc1"]
    if own in stores:
        return stores[own], own
    if COMMON_STORE in stores:
        return stores[COMMON_STORE], COMMON_STORE
    return None, None


def _tc_ask(codes):
    """{external_code: закупка ТК} — та самая база, от которой платформа считает цену продажи.

    Батч 100 (жёстко). Неизвестный платформе код валит ВЕСЬ батч 422 с перечнем позиций
    `external_codes.<индекс>` — такие выкидываем и переспрашиваем остаток.
    `buy_price <= 0` = цены нет (ноля в прайсе не существует, это сбой выдачи).
    """
    key = os.getenv("CARTRIDGE_API_KEY")
    out = {}
    if not key or not codes:
        return out
    todo = [list(codes)[i:i + TC_BATCH] for i in range(0, len(codes), TC_BATCH)]
    while todo:
        batch = todo.pop()
        for _ in range(3):
            r = requests.post(TC_API, headers={"Api-Key": key},
                              json={"external_codes": batch}, timeout=60)
            if r.status_code == 422:
                bad = {int(m.group(1)) for m in
                       (re.match(r"external_codes\.(\d+)$", k) for k in (r.json().get("errors") or {}))
                       if m}
                batch = [c for i, c in enumerate(batch) if i not in bad]
                if not batch:
                    break
                continue
            if r.status_code == 429:
                time.sleep(8)
                continue
            r.raise_for_status()
            for code, v in (r.json() or {}).items():
                p = (v or {}).get("buy_price")
                if p is not None and float(p) > 0:
                    out[code] = float(p)
            break
    return out


def code_variants(code):
    """Код как на площадке и он же без ведущих нулей — какой из них знает ТК, зависит от карточки."""
    v = [code]
    bare = code.lstrip("0")
    if bare and bare != code:
        v.append(bare)
    return v


def set_components(codes):
    """{код набора: [внешние коды компонентов]} — справочник состава наборов.

    Сначала кэш коллектора (таблица `set_cost`, её ведёт collectors/set_cost.py), затем добор
    живым `mix_data` по кодам, которых в кэше нет: карточка-набор могла появиться позже
    ночного прогона. Простой артикул платформа отдаёт как {"error": "not_mix"}.
    """
    keys = set(codes) | {c[:4] for c in codes if re.match(r"^\d{4}", c)}
    out = {r["external_code"]: [str(x) for x in (r["components"] or [])]
           for r in db.query("SELECT external_code, components FROM set_cost "
                             "WHERE components IS NOT NULL AND external_code = ANY(%s)",
                             (list(keys),))}
    key = os.getenv("CARTRIDGE_API_KEY")
    for c in sorted(keys - set(out)) if key else []:
        try:
            r = requests.post(MIX_API, headers={"Api-Key": key},
                              json={"external_code": c}, timeout=30)
            d = r.json() if r.status_code == 200 else None
        except Exception:
            d = None
        if isinstance(d, list) and d:
            out[c] = [str(x) for x in d]
    return {k: v for k, v in out.items() if v}


def tc_cost(codes):
    """(цены ТК по самому коду, по базовому 4-значному коду, по составу набора).

    Артикул площадки часто = «<4 цифры базового кода><вариант>» (15301, 153010): платформа
    такого кода не знает, знает базовый 1530. Производные карточки — тот же товар, себест
    у них равен себесту базового кода (решение Сергея 23.08.2026); тот же префиксный мост,
    что в margin_control. В отчёте помечается «ТК-база», чтобы было видно происхождение.

    Набор — одна карточка площадки из нескольких товаров (3241 = 3237+3238+3239+3240):
    своей закупки у ТК на такой код нет, себест собирается ПО КОМПОНЕНТАМ, по живой закупке
    каждого (задача Сергея 23.08.2026). Неполный состав (хоть у одного компонента цены нет)
    не берём вовсе — сумма вышла бы заниженной, а заниженный себест выпускает товар в минус.
    """
    codes = {v for c in codes for v in code_variants(c)}
    exact = _tc_ask(codes)
    left = {c for c in codes if c not in exact and re.match(r"^\d{4}", c)}
    approx = _tc_ask({c[:4] for c in left})
    comps = set_components({c for c in left if c[:4] not in approx})
    parts = _tc_ask({x for v in comps.values() for x in v})
    sets = {k: sum(parts[x] for x in v) for k, v in comps.items() if all(x in parts for x in v)}
    return exact, approx, sets


def cogs_unit(code, acc, cm, tc):
    """Себест единицы: закупка ТК → закупка базового кода → сумма состава набора → остаток МС.

    Каждый шаг пробуется на обоих написаниях кода (с ведущим нулём и без).
    """
    exact, base, sets = tc
    if not code:
        return None, None
    for c in code_variants(code):
        if c in exact:
            return exact[c], "ТК"
    for c in code_variants(code):
        if c[:4] in base:
            return base[c[:4]], "ТК-база"
        if c in sets:
            return sets[c], "ТК-набор"
        if c[:4] in sets:
            return sets[c[:4]], "ТК-набор"
    for c in code_variants(code):
        unit, src = cost_for(c, acc, cm)
        if unit is not None:
            return unit, src
    return None, None


# ─── карантин по площадкам ──────────────────────────────────────────────────────────────
def wb_quarantine(acc):
    h, off, out = {"Authorization": wb_token(acc)}, 0, []
    while True:
        r = requests.get(f"{WB_API}/api/v2/quarantine/goods", headers=h,
                         params={"limit": 1000, "offset": off}, timeout=60)
        r.raise_for_status()
        g = (r.json().get("data") or {}).get("quarantineGoods") or []
        out += g
        if len(g) < 1000:
            return out
        off += 1000


def ya_quarantine():
    key, biz = os.getenv("YANDEX_API_KEY_ACC1"), os.getenv("YANDEX_BUSINESS_ID_ACC1")
    H = {"Api-Key": key, "Content-Type": "application/json"}
    tok, out = None, []
    while True:
        p = {"limit": 200}
        if tok:
            p["page_token"] = tok
        r = requests.post(f"{YA_API}/businesses/{biz}/price-quarantine", headers=H,
                          json={}, params=p, timeout=60)
        r.raise_for_status()
        res = r.json().get("result") or {}
        out += res.get("offers") or []
        tok = (res.get("paging") or {}).get("nextPageToken")
        if not tok:
            return out


YA_OFFER = re.compile(r"^(\d+)(?:X(\d+))?$")


def ya_code(offer_id):
    """offerId Маркета → (внешний код МС, штук в комплекте). '0123X2' → ('0123', 2).

    Ведущий ноль ЗНАЧАЩИЙ: внешний код в МС и у ТК — ровно «0123», кода «123» они не знают
    (23.08.2026 срезанный ноль оставил 35 карточек Маркета без себеста). Вариант без нуля
    остаётся запасным — см. code_variants.
    """
    m = YA_OFFER.match(offer_id or "")
    if not m:
        return None, 1
    return m.group(1), int(m.group(2) or 1)


def ya_verdict_prices(offer):
    """Цены из params вердикта — для записей, у которых верхнеуровневых цен нет вовсе.

    У вердикта LOW_PRICE Маркет не кладёт ни `currentPrice`, ни `lastValidPrice`: цена лежит
    только внутри `verdicts[].params` (CURRENT_PRICE, MIN_PRICE). Без разбора params такая
    карточка получала цену 0 и вердикт «НЕТ ЦЕНЫ» — то есть навсегда оставалась в карантине
    (случай 2343, 23.08.2026). Берём ПОСЛЕДНИЙ вердикт: он свежее прочих.
    """
    cur = old = 0.0
    for v in (offer.get("verdicts") or []):
        pr = {p.get("name"): p.get("value") for p in (v.get("params") or [])}
        for name, key in (("CURRENT_PRICE", "cur"), ("LAST_VALID_PRICE", "old"), ("MIN_PRICE", "old")):
            if pr.get(name):
                val = float(pr[name])
                if key == "cur":
                    cur = val
                elif not old or name == "LAST_VALID_PRICE":
                    old = val
    return {"cur": cur, "old": old}


# ─── экономика и вердикт ────────────────────────────────────────────────────────────────
def verdict(price, cogs, ret):
    """(вердикт, прибыль ₽, пол ₽) для карантинной цены."""
    if not price:
        return "НЕТ ЦЕНЫ", None, None      # площадка держит карточку без цены — считать нечего
    if cogs is None:
        return "НЕТ СЕБЕСТА", None, None   # ни закупки у ТК, ни остатка в МС
    net = price * (1 - ret) - cogs
    floor = price * FLOOR_PCT
    return ("ОК" if net >= floor else "СТОП"), net, floor


# ─── выпуск из карантина ────────────────────────────────────────────────────────────────
def _wb_h(acc):
    return {"Authorization": wb_token(acc), "Content-Type": "application/json"}


def _wb_get(acc, path, params):
    """GET с пейсингом и отступом на 429/5xx — лимит WB общий на эндпоинт.

    503 у WB прилетает пачками и проходит само. Без повтора одна такая отбивка
    роняла весь прогон вместе с несохранённым журналом лестницы (26.08.2026).
    """
    last = None
    for attempt in range(5):
        time.sleep(WB_PACE)
        try:
            r = requests.get(f"{WB_API}{path}", headers=_wb_h(acc), params=params, timeout=60)
        except requests.RequestException as e:            # сеть моргнула — ещё раз
            last = e
            time.sleep(2 ** attempt * 3)
            continue
        if r.status_code != 429 and r.status_code < 500:
            r.raise_for_status()
            return r.json()
        last = requests.HTTPError(f"HTTP {r.status_code} {r.text[:120]}", response=r)
        time.sleep(2 ** attempt * 3)
    raise last


def wb_current(acc, nm):
    """Текущая цена карточки на WB: (база, скидка %, цена покупателю) или None."""
    g = ((_wb_get(acc, "/api/v2/list/goods/filter",
                  {"filterNmID": nm, "limit": 10}).get("data") or {}).get("listGoods") or [])
    if not g:
        return None
    sz = (g[0].get("sizes") or [{}])[0]
    return (float(sz.get("price") or 0), float(g[0].get("discount") or 0),
            float(sz.get("discountedPrice") or 0))


def wb_push(acc, nm, price):
    """Отправить цену и дождаться вердикта задачи. → (ok, текст ошибки)."""
    body = {"data": [{"nmID": int(nm), "price": int(price), "discount": 0}]}
    for attempt in range(4):                              # 5xx — не отказ от цены, а сбой WB;
        try:                                              # иначе лестница зря мельчит шаг
            r = requests.post(f"{WB_API}/api/v2/upload/task", headers=_wb_h(acc),
                              json=body, timeout=60)
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
    err = "вердикта задачи не дождались"
    for _ in range(12):                                   # задача обрабатывается асинхронно
        time.sleep(3)
        try:
            d = _wb_get(acc, "/api/v2/history/tasks", {"uploadID": uid}).get("data") or {}
        except Exception as e:                            # сеть/лимит — просто ещё раз
            err = str(e)[:120]
            continue
        if isinstance(d, list):                           # у WB здесь ОБЪЕКТ, но подстрахуемся
            d = d[0] if d else {}
        st = d.get("status")
        if st in (1, 2):                                  # в очереди / в работе
            continue
        if (d.get("successGoodsNumber") or 0) > 0:
            return True, ""
        return False, f"status={st} {_wb_task_err(acc, uid)}".strip()
    return False, err


def _wb_task_err(acc, uid):
    """Текст ошибки по товарам задачи (у WB он лежит отдельным методом)."""
    try:
        it = ((_wb_get(acc, "/api/v2/history/goods/task",
                       {"uploadID": uid, "limit": 10}).get("data") or {}).get("historyGoods") or [])
        return (it[0].get("errorText") or "") if it else ""
    except Exception:
        return ""


def wb_release(acc, nm, target, log, state):
    """Лестница к целевой цене ТК. → (итог, шагов, конечная цена покупателю).

    Направление берём по факту: в карантин сажает и падение, и рост цены. Промежуточные
    ступени всегда между старой и целевой ценой, поэтому по дороге товар не продаётся
    дешевле цели (при падении) и не дороже её (при росте).

    Прошла ступень или нет — решает ПЕРЕЧИТКА цены карточки, а не вердикт задачи: вердикт
    приходит асинхронно и его формат уже один раз подвёл. Размер шага `state` общий на прогон,
    чтобы отбитый крупный шаг стоил одного отказа на все карточки, а не на каждую.
    """
    cur = wb_current(acc, nm)
    if not cur:
        return "НЕ НАЙДЕН", 0, None
    buyer = cur[2] or cur[0]
    steps = 0
    while abs(buyer - target) > target * 0.005 and steps < WB_MAX_STEPS:
        down = buyer > target
        k = WB_STEPS[state["i"]]
        nxt = max(target, buyer * (1 - k)) if down else min(target, buyer * (1 + k))
        if abs(nxt - target) < 1:
            nxt = int(round(target))                      # финальная ступень — ровно цена ТК
        else:
            nxt = int(nxt + 0.999) if down else int(nxt)  # промежуточную округляем в свою пользу
        if down and nxt >= int(buyer):
            nxt = int(buyer) - 1                          # цена не двигается — минимальный шаг
        if not down and nxt <= int(buyer):
            nxt = int(buyer) + 1
        ok, err = wb_push(acc, nm, nxt)
        steps += 1
        cur = wb_current(acc, nm)
        new_buyer = (cur[2] or cur[0]) if cur else buyer
        if abs(new_buyer - nxt) > 1 and ok:               # задача принята, цена ещё не доехала
            time.sleep(4)
            cur = wb_current(acc, nm)
            new_buyer = (cur[2] or cur[0]) if cur else buyer
        if abs(new_buyer - nxt) <= 1:                     # ступень встала на карточку
            log.append(dict(platform="wb", account=acc, id=nm, action=f"шаг {nxt}",
                            result="применён", detail=f"{buyer:.0f} → {new_buyer:.0f}"))
            buyer = new_buyer
            continue
        if state["i"] + 1 < len(WB_STEPS):                # шаг отбит — дальше идём мельче
            log.append(dict(platform="wb", account=acc, id=nm, action=f"шаг {nxt}",
                            result=f"отказ, шаг −{WB_STEPS[state['i']]:.0%} → −{WB_STEPS[state['i'] + 1]:.0%}",
                            detail=(err or "цена на карточке не изменилась")[:160]))
            state["i"] += 1
            continue
        return f"ОТКАЗ: {(err or 'цена не изменилась')[:120]}", steps, buyer
    done = abs(buyer - target) <= target * 0.005
    return ("ВЫПУЩЕН" if done else "НЕ ДОШЁЛ"), steps, buyer


def ya_confirm(offer_ids):
    """Штатный выпуск Маркета: подтвердить карантинные цены пачками до 200 offerId."""
    key, biz = os.getenv("YANDEX_API_KEY_ACC1"), os.getenv("YANDEX_BUSINESS_ID_ACC1")
    H = {"Api-Key": key, "Content-Type": "application/json"}
    done, errs = 0, []
    for i in range(0, len(offer_ids), 200):
        chunk = offer_ids[i:i + 200]
        r = requests.post(f"{YA_API}/businesses/{biz}/price-quarantine/confirm", headers=H,
                          json={"offerIds": chunk}, timeout=60)
        if r.status_code >= 400:
            errs.append(f"HTTP {r.status_code} {r.text[:200]}")
            continue
        done += len(chunk)
        time.sleep(1)
    return done, errs


TARGETS = BASE_DIR / "docs" / "reports" / "quarantine_targets.csv"
TG_COLS = ["account", "id", "target", "status", "updated"]


def targets_load():
    """Журнал целей WB: недоигранная лестница обязана пережить перезапуск.

    Как только первая ступень применилась, карточка ИСЧЕЗАЕТ из списка карантина WB, хотя
    стоит ещё не по цене ТК. Без журнала цель теряется и товар остаётся на промежуточной
    ступени навсегда (наступили на это 23.08.2026).
    """
    if not TARGETS.exists():
        return {}
    with TARGETS.open(encoding="utf-8") as f:
        return {(r["account"], r["id"]): r for r in csv.DictReader(f)}


def targets_save(t):
    with TARGETS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TG_COLS)
        w.writeheader()
        w.writerows(t.values())


def apply_ok(rows, log=None):
    """Выпустить из карантина всё, что прошло проверку по марже.

    `log` принимаем снаружи, чтобы при аварии journal всё равно лёг на диск:
    отправленные цены обязаны иметь запись, даже если прогон не дожил до конца.
    """
    log = [] if log is None else log
    ya = [r["id"] for r in rows if r["platform"] == "ya" and r["verdict"] == "ОК"]
    if ya:
        done, errs = ya_confirm(ya)
        log.append(dict(platform="ya", account="ya_acc1", id="", action=f"confirm {len(ya)} шт",
                        result=f"подтверждено {done}", detail="; ".join(errs)[:160]))
        print(f"Маркет: подтверждено {done} из {len(ya)}" + (f", ошибок {len(errs)}" if errs else ""))

    day = datetime.date.today().isoformat()
    tg = targets_load()
    for r in rows:                                        # новые «ОК» — в журнал целей
        if r["platform"] == "wb" and r["verdict"] == "ОК":
            k = (r["account"], str(r["id"]))
            if tg.get(k, {}).get("status") != "ВЫПУЩЕН":
                tg[k] = dict(account=r["account"], id=str(r["id"]),
                             target=f"{r['price_new']:.0f}", status="в работе", updated=day)
    work = [v for v in tg.values() if v["status"] != "ВЫПУЩЕН"]

    res, state = {}, {"i": 0}                             # размер шага общий на весь прогон
    for v in work:
        try:
            st, steps, fin = wb_release(v["account"], v["id"], float(v["target"]), log, state)
        except Exception as e:                            # одна карточка не роняет прогон
            st, steps, fin = f"СБОЙ: {str(e)[:60]}", 0, "?"
        res[st] = res.get(st, 0) + 1
        v["status"], v["updated"] = ("ВЫПУЩЕН" if st == "ВЫПУЩЕН" else st[:40]), day
        log.append(dict(platform="wb", account=v["account"], id=v["id"], action="итог",
                        result=st, detail=f"цель {v['target']}, шагов {steps}, стало {fin}"))
        targets_save(tg)                                  # состояние пишем по ходу, не в конце
    if work:
        print("WB лестница: " + ", ".join(f"{k} {v}" for k, v in sorted(res.items())))
    return log


def build():
    ret, month = retention()
    ya_bands = ya_retention_bands()
    cm = cost_map()
    wb_vc = {r["nm_id"]: r["vendor_code"] for r in db.query("SELECT nm_id, vendor_code FROM wb_price")}
    raw = []

    for acc in ("wb_acc1", "wb_acc2"):
        for g in wb_quarantine(acc):
            raw.append(dict(platform="wb", account=acc, id=g["nmID"], code=wb_vc.get(g["nmID"]) or "",
                            pack=1,
                            price=float(g["newPrice"]) * (1 - float(g.get("newDiscount") or 0) / 100),
                            old=float(g["oldPrice"]) * (1 - float(g.get("oldDiscount") or 0) / 100),
                            reason=""))

    ya_offers = ya_quarantine()
    arch = ya_archived([o.get("offerId") for o in ya_offers if o.get("offerId")])
    if arch:
        print(f"Маркет: архивных офферов пропущено {len(arch)} ({', '.join(sorted(arch))})")
    for o in ya_offers:
        if o.get("offerId") in arch:
            continue
        code, pack = ya_code(o.get("offerId"))
        vp = ya_verdict_prices(o)
        raw.append(dict(platform="ya", account="ya_acc1", id=o.get("offerId"), code=code or "",
                        pack=pack,
                        price=float((o.get("currentPrice") or {}).get("value") or 0) or vp["cur"],
                        old=float((o.get("lastValidPrice") or {}).get("value") or 0) or vp["old"],
                        reason=",".join(sorted({x.get("type") for x in (o.get("verdicts") or [])}))))

    tc = tc_cost({r["code"] for r in raw if r["code"]})               # один заход на все коды

    rows = []
    for r in raw:
        unit, src = cogs_unit(r["code"], r["account"], cm, tc)
        cogs = unit * r["pack"] if unit is not None else None
        rate = (ya_rate(ya_bands, ret[r["account"]], r["price"]) if r["platform"] == "ya"
                else ret[r["account"]])
        v, net, floor = verdict(r["price"], cogs, rate)
        rows.append(dict(platform=r["platform"], account=r["account"], id=r["id"], code=r["code"],
                         pack=r["pack"], price_new=round(r["price"], 2), price_old=round(r["old"], 2),
                         cogs=round(cogs, 2) if cogs else None, cost_src=src or "",
                         retention_pct=round(rate * 100, 1),
                         net=round(net, 2) if net is not None else None,
                         floor=round(floor, 2) if floor is not None else None,
                         verdict=v, reason=r["reason"]))
    return rows, month


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="выпустить из карантина всё с вердиктом ОК (Маркет — confirm, WB — лестница)")
    a = ap.parse_args()

    rows, month = build()
    day = datetime.date.today().isoformat()
    out = BASE_DIR / "docs" / "reports" / f"quarantine_check_{day}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"Карантин цен на {day} (удержания — по месяцу «{month}»)")
    print(f"{'площадка':10} {'всего':>6} {'ОК':>5} {'СТОП':>6} {'нет себеста':>12}")
    for acc in sorted({r["account"] for r in rows}):
        s = [r for r in rows if r["account"] == acc]
        c = lambda v: sum(1 for r in s if r["verdict"] == v)          # noqa: E731
        print(f"{acc:10} {len(s):6} {c('ОК'):5} {c('СТОП'):6} {c('НЕТ СЕБЕСТА') + c('НЕТ ЦЕНЫ'):12}")
    stop = [r for r in rows if r["verdict"] == "СТОП"]
    if stop:
        loss = sum(r["floor"] - r["net"] for r in stop)
        print(f"СТОП суммарно недобирают до пола {loss:,.0f} ₽ на единицу товара")
    print(f"файл: {out.relative_to(BASE_DIR)}")

    if a.apply:
        log = []
        try:
            apply_ok(rows, log)
        finally:
            if log:
                lf = BASE_DIR / "docs" / "reports" / f"quarantine_apply_{day}.csv"
                with lf.open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=["platform", "account", "id",
                                                      "action", "result", "detail"])
                    w.writeheader()
                    w.writerows(log)
                print(f"журнал выпуска: {lf.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()

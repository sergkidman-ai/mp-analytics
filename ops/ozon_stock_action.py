"""ops/ozon_stock_action.py — поток: mkt

Автоучастие в акциях Ozon «Распродажа стока» (и только в них — белый список по названию).

Зачем: работаем по ФБС, но на складах Ozon оседают возвраты и невыкупы. Пока товар лежит,
он копит платное хранение; акция стока — способ отдать его дороже, чем он уйдёт по вывозу.

Что делает:
  1. берёт акции аккаунта, оставляет только те, чьё название в белом списке;
  2. берёт кандидатов, оставляет тех, у кого РЕАЛЬНЫЙ остаток на складах Ozon >= min_stock
     (поле stock у кандидата врёт, пока аккаунт в акции не участвует — проверено 22.08.2026);
  3. считает свою цену, а не потолок Ozon:
       пол   = (себестоимость + 300 ₽) / доля выручки, что остаётся нам после удержаний;
       старт = потолок Ozon (он всегда ниже цены товара в других акциях — проверено),
               со страховкой: если в другой акции цена ниже — встаём под неё;
       шаг   = четверть расстояния потолок→пол, раз в неделю, 4 снижения;
       ступень лестницы хранится на ТОВАРЕ и переезжает в следующую акцию (акция живёт 14 дней);
       пол выше потолка → товар не заводим вовсе (лежит дальше, ждёт вывоза остатков);
  4. по --apply отправляет в Ozon, пишет журнал и отчёт в бот.

Себестоимость — цепочка (см. память cogs-lookup-chain): возврат/невыкуп этого кода →
последняя отгрузка Ozon этого кода → материнская карточка (первые 4 цифры внешнего кода) →
универсальная модель через prc_tc_link → справочник наборов → живая закупка ТК.

Запуск:
    ./venv/bin/python ops/ozon_stock_action.py plan               # расчёт, ничего не шлём
    ./venv/bin/python ops/ozon_stock_action.py apply              # заведение в акцию
    ./venv/bin/python ops/ozon_stock_action.py watch --notify     # сторож: новая акция / старт завтра
"""
import os
import re
import sys
import time
import json
import argparse
import datetime as dt
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = "https://api-seller.ozon.ru"
CRED_ENV = {"oz_acc1": ("OZON_CLIENT_ID_ACC1", "OZON_API_KEY_ACC1"),
            "oz_acc2": ("OZON_CLIENT_ID_ACC2", "OZON_API_KEY_ACC2")}
ACCOUNTS = ["oz_acc1", "oz_acc2"]

# --- политика (решения Сергея 22.08.2026) -----------------------------------
WHITELIST = ("распродажа стока",    # (региональные включены обратно 22.08 по варианту 1)    # тип акции опознаём ТОЛЬКО по названию: action_type
              "акция для склад")    # бесполезен — под STOCK_DISCOUNT сидят все именные акции.
                                    # «акция для склад» ловит региональное семейство («География»
                                    # в ЛК): «Акция для складов. <регион>» + «для склада в <город>»
GOAL_NET = 300.0                    # сколько минимум хотим оставить себе с единицы, ₽
STEPS = 4                           # снижений от потолка до пола
STEP_DAYS = 7                       # шаг раз в неделю
FLAT_EPS = 0.05                     # ход меньше 5% потолка → лестницу не строим, стоим на потолке
KEEP_FALLBACK = {"oz_acc1": 0.421, "oz_acc2": 0.459}   # доля, что остаётся после удержаний Ozon
SERGEY_CHAT_ID = 1031321444         # только явный chat_id, см. память telegram-channels
UNDERCUT = ("распродажа стока",)    # где встаём на 1 ₽ ниже цены товара в ЧУЖИХ акциях.
                                    # Только распродажа стока: там цель — слить лежачий товар.
                                    # На региональных подрезка била бы по ходовому ассортименту
                                    # (решение Сергея 22.08: вариант 1 — без подрезки).
RET_STATUS = ("unredeemed", "return_stock", "return_ozon", "return_defect")


def _headers(account):
    cid_env, key_env = CRED_ENV[account]
    cid, key = os.getenv(cid_env), os.getenv(key_env)
    if not cid or not key:
        raise RuntimeError(f"{cid_env}/{key_env} не заданы в .env")
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}


def _req(account, method, path, body=None, tries=4):
    """Запрос к Ozon с обработкой 429."""
    H = _headers(account)
    for i in range(tries):
        r = (requests.get(API + path, headers=H, timeout=90) if method == "GET"
             else requests.post(API + path, headers=H, json=body or {}, timeout=90))
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{path}: не удалось за {tries} попыток")


def is_whitelisted(title):
    return _has(title, WHITELIST)


def _has(title, words):
    t = (title or "").lower().replace("ё", "е")
    return any(w.replace("ё", "е") in t for w in words)


# --- данные аккаунта --------------------------------------------------------
def actions(account):
    return _req(account, "GET", "/v1/actions").get("result", []) or []


def candidates(account, action_id):
    """Кандидаты акции (пагинация)."""
    out, offset = [], 0
    while True:
        res = _req(account, "POST", "/v1/actions/candidates",
                   {"action_id": action_id, "limit": 100, "offset": offset}).get("result", {})
        got = res.get("products", []) or []
        out += got
        offset += len(got)
        if len(got) < 100 or offset >= (res.get("total") or 0):
            return out


def participants(account, action_id):
    """Товары, уже заведённые в акцию."""
    out, offset = [], 0
    while True:
        res = _req(account, "POST", "/v1/actions/products",
                   {"action_id": action_id, "limit": 100, "offset": offset}).get("result", {})
        got = res.get("products", []) or []
        out += got
        offset += len(got)
        if len(got) < 100 or offset >= (res.get("total") or 0):
            return out


def offer_of(account, product_ids):
    """product_id → (offer_id, name)."""
    out = {}
    ids = list(product_ids)
    for k in range(0, len(ids), 100):
        for p in _req(account, "POST", "/v3/product/info/list",
                      {"product_id": ids[k:k + 100]}).get("items", []):
            out[p["id"]] = (p.get("offer_id"), (p.get("name") or "")[:60])
    return out


def ozon_stock(account):
    """offer_id → свободный остаток НА СКЛАДАХ OZON (это и есть возвраты/невыкупы при ФБС)."""
    sku2offer, pids, last = {}, [], ""
    while True:
        res = _req(account, "POST", "/v3/product/list",
                   {"filter": {"visibility": "ALL"}, "last_id": last, "limit": 1000}).get("result", {})
        items = res.get("items", []) or []
        pids += [i["product_id"] for i in items]
        last = res.get("last_id") or ""
        if len(items) < 1000:
            break
    for k in range(0, len(pids), 1000):
        for it in _req(account, "POST", "/v3/product/info/list",
                       {"product_id": pids[k:k + 1000]}).get("items", []):
            off = it.get("offer_id")
            if not off:
                continue
            if it.get("sku"):
                sku2offer[str(it["sku"])] = off
            for s in (it.get("sources") or []):
                if s.get("sku"):
                    sku2offer[str(s["sku"])] = off
    stock, offset = {}, 0
    while True:
        res = _req(account, "POST", "/v2/analytics/stock_on_warehouses",
                   {"limit": 1000, "offset": offset, "warehouse_type": "ALL"}).get("result", {})
        rows = res.get("rows", []) or []
        for r in rows:
            off = sku2offer.get(str(r.get("sku")))
            if off:
                stock[off] = stock.get(off, 0) + int(r.get("free_to_sell_amount") or 0)
        offset += len(rows)
        if len(rows) < 1000:
            return stock


def keep_ratio(account):
    """Доля выручки, что остаётся нам: 1 − удержания/продажи за последний ЗАКРЫТЫЙ месяц."""
    try:
        import reports.ozon_mp_report as R
        today = dt.date.today()
        first = today.replace(day=1)
        prev = first - dt.timedelta(days=1)
        b = R.balance(account, prev.year, prev.month)
        sales = abs(float(b.get("sales") or 0))
        itog = sum(abs(float(b.get(k) or 0)) for k in R.EXP_SVC)
        if sales > 0:
            k = 1 - itog / sales
            if 0.2 < k < 0.9:
                return round(k, 4), f"{prev:%m.%Y}"
    except Exception as e:                                    # noqa: BLE001
        print(f"  [keep] отчёт недоступен ({e}), берём константу", flush=True)
    return KEEP_FALLBACK.get(account, 0.42), "константа"


# --- себестоимость ----------------------------------------------------------
def _parent(code):
    m = re.match(r"^(\d{4})", code or "")
    return m.group(1) if m and m.group(1) != code else None


def cogs_map(offers):
    """{offer_id: (себест/шт, источник)} по цепочке фолбэков (см. память cogs-lookup-chain)."""
    offers = [o for o in offers if o]
    base = sorted(set(offers) | {p for p in (_parent(o) for o in offers) if p})
    link = {}
    for r in db.query("select external_code ec, ref_code rc from prc_tc_link "
                      "where external_code = any(%s)", (base,)):
        link.setdefault(r["ec"], []).append(r["rc"])
    allc = sorted(set(base) | {c for v in link.values() for c in v})
    ship = {}
    for r in db.query("""
            select p.external_code ec, c.demand_date dt, c.status,
                   (dp.cost / nullif(dp.qty, 0)) unit
            from ms_product p
            join ms_demand_pos dp on dp.ms_id = p.ms_id
            join oz_cogs_demand c on c.demand_id = dp.demand_id
            where p.external_code = any(%s) and dp.qty > 0 and dp.cost > 0
            order by c.demand_date desc""", (allc,)):
        ship.setdefault(r["ec"], []).append(r)
    sets = {r["external_code"]: float(r["cost"]) for r in db.query(
        "select external_code, cost from set_cost "
        "where external_code = any(%s) and cost is not null", (allc,))}
    tc = {r["external_code"]: float(r["buy_price"]) for r in db.query(
        "select external_code, buy_price from tc_buy_price_latest "
        "where external_code = any(%s) and buy_price is not null", (allc,))}

    def by_ship(code):
        """Себест по отгрузкам Ozon кода: сперва возврат (это тот самый лежащий товар), потом свежее."""
        rs = ship.get(code, [])
        for r in rs:
            if r["status"] in RET_STATUS:
                return float(r["unit"]), f"возврат {r['dt']:%d.%m.%y} {code}"
        if rs:
            return float(rs[0]["unit"]), f"отгрузка {rs[0]['dt']:%d.%m.%y} {code}"
        return None, None

    out = {}
    for o in offers:
        par = _parent(o)
        v, s = by_ship(o)
        if v is None and par:
            v, s = by_ship(par)
            if v:
                s += " (родитель)"
        if v is None:                                    # универсальная модель из справочника ТК
            for src in [o] + ([par] if par else []):
                cand = [(c, *by_ship(c)) for c in link.get(src, [])]
                cand = [(c, x, m) for c, x, m in cand if x]
                if cand:
                    c, v, s = max(cand, key=lambda t: ship[t[0]][0]["dt"])
                    s += " (универсальная)"
                    break
        if v is None:
            for c in [o] + ([par] if par else []):
                if c in sets:
                    v, s = sets[c], f"набор {c}"
                    break
        if v is None:
            for c in [o] + ([par] if par else []):
                if c in tc:
                    v, s = tc[c], f"закупка ТК {c}"      # оценка, а не себест лежащего лота
                    break
        out[o] = (v, s or "НЕТ")
    return out


# --- лестница ---------------------------------------------------------------
def ladder_state(account, offers):
    return {r["offer_id"]: r for r in db.query(
        "select * from oz_action_ladder where account = %s and offer_id = any(%s)",
        (account, list(offers)))}


def price_for(cap, floor, rung):
    """Цена ступени: от потолка вниз к полу, но не ниже пола."""
    if cap <= 0 or floor > cap:
        return None, "пол выше потолка"
    if (cap - floor) < FLAT_EPS * cap:
        return round(cap, 2), "фикс потолок (ход меньше 5%)"
    p = cap - rung * (cap - floor) / STEPS
    return round(max(p, floor), 2), f"ступень {rung}/{STEPS}"


def other_action_min(account, skip_action_id):
    """{offer_id: минимальная цена товара в ЧУЖИХ акциях} — под неё и встаём, не выше.

    Свои белые акции исключены: товар — кандидат сразу в нескольких региональных, и они
    подрезали бы друг друга по рублю за прогон, уводя цену вниз помимо лестницы. Между
    своими акциями держим ОДНУ цену (её задаёт лестница), подрезаемся только под чужие."""
    out = {}
    for a in actions(account):
        aid = a.get("id")
        if aid == skip_action_id or not a.get("is_participating"):
            continue
        if is_whitelisted(a.get("title")):        # свои акции ведём сами, см. докстринг
            continue
        try:
            prods = participants(account, aid)
        except Exception:                                 # noqa: BLE001
            continue
        pid2 = offer_of(account, [p["id"] for p in prods]) if prods else {}
        for p in prods:
            off = (pid2.get(p["id"]) or (None,))[0]
            pr = float(p.get("action_price") or 0)
            if off and pr > 0:
                out[off] = min(out.get(off, pr), pr)
    return out


# --- основной расчёт --------------------------------------------------------
def _decide(row, keep, state, others, today, undercut=True):
    """Считает пол, ступень и целевую цену. Заполняет row: floor/rung/price/why/skip."""
    off, cap = row["offer_id"], row["cap"]
    v = row.get("cogs")
    if v is None:
        row["skip"] = "себестоимость не найдена"
        return row
    floor = (v + GOAL_NET) / keep
    if undercut:
        om = others.get(off)
        if om and om - 1 < cap:                 # в чужой акции дешевле — встаём под неё
            cap, row["capped_by_other"] = om - 1, om
    st = state.get(off) or {}
    if st.get("cap_price") is not None:         # потолок лестницы зафиксирован при заведении
        cap = min(cap, float(st["cap_price"]))
    elif row["inside"] and (row.get("now_price") or 0) > 0:
        # товар завели руками — лестница стартует от ЕГО цены, а не от потолка акции:
        # цену не дёргаем, просто берём под управление. Ниже пола не оставляем.
        cap = max(floor, min(cap, float(row["now_price"])))
    row["cap"] = round(cap, 2)
    rung = int(st.get("rung") or 0)
    last = st.get("last_step_on")
    if last and (today - last).days >= STEP_DAYS and rung < STEPS:
        rung += 1
        row["stepped"] = True
    row["floor"], row["rung"] = round(floor, 2), rung
    price, why = price_for(cap, floor, rung)
    row["price"], row["why"] = price, why
    if price is None:
        row["skip"] = f"пол {floor:.0f} > потолок {cap:.0f}"
    return row


def plan_account(account):
    """Решение по каждому товару белых акций: add / update / keep / remove / skip."""
    keep, keep_src = keep_ratio(account)
    acts = [a for a in actions(account) if is_whitelisted(a.get("title"))]
    if not acts:
        return [], keep, keep_src, []
    stock = ozon_stock(account)
    today = dt.date.today()
    # Чужие цены одинаковы для всех наших акций (свои из подрезки исключены) — читаем один раз:
    # иначе каждая белая акция заново вычитывает участников всех остальных (тысячи позиций).
    others = other_action_min(account, None)
    plans = []
    for a in acts:
        aid, title = a["id"], a.get("title")
        cands = candidates(account, aid)
        inside = participants(account, aid) if a.get("is_participating") else []
        pid2 = offer_of(account, [c["id"] for c in cands] + [p["id"] for p in inside])
        offs = [pid2.get(x["id"], (None,))[0] for x in cands + inside]
        cogs = cogs_map(offs)
        state = ladder_state(account, [o for o in offs if o])

        for src, items in (("add", cands), ("inside", inside)):
            for c in items:
                off, nm = pid2.get(c["id"], (None, ""))
                row = {"account": account, "action_id": aid, "action_title": title,
                       "product_id": c["id"], "offer_id": off, "name": nm,
                       "cap": float(c.get("max_action_price") or 0),
                       "min_stock": int(c.get("min_stock") or 0),
                       "stock": int(stock.get(off, 0)) if off else 0,
                       "now_price": float(c.get("action_price") or 0) if src == "inside" else None,
                       "inside": src == "inside", "mode": None}
                plans.append(row)
                if not off:
                    row["skip"] = "нет offer_id"
                    continue
                row["cogs"], row["cogs_src"] = cogs.get(off, (None, "НЕТ"))
                need = max(row["min_stock"], 1)
                if row["inside"] and row["stock"] == 0:
                    # на складе Ozon пусто: снимаем, иначе заказы по акционной цене
                    # мигрируют на наш склад FBS (решение Сергея 22.08)
                    row["skip"], row["mode"] = "на складе Ozon не осталось", "remove"
                    continue
                if row["stock"] < need and not row["inside"]:
                    row["skip"] = f"остаток {row['stock']} < нужно {need}"
                    continue
                _decide(row, keep, state, others, today, undercut=_has(title, UNDERCUT))
                if row.get("skip"):
                    # цена ниже нашего порога: нового не заводим, заведённого снимаем
                    row["mode"] = "remove" if row["inside"] and "пол" in row["skip"] else None
                    continue
                if not row["inside"]:
                    row["mode"] = "add"
                elif abs((row["now_price"] or 0) - row["price"]) >= 1:
                    row["mode"] = "update"
                else:
                    row["mode"] = "keep"
    return plans, keep, keep_src, acts


def _remember(account, aid, p, today):
    db.execute("""
        insert into oz_action_ladder (account, offer_id, rung, floor_price, cap_price,
                                      last_price, cogs, cogs_source, last_action_id,
                                      last_step_on, updated_at)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (account, offer_id) do update set
            rung = excluded.rung, floor_price = excluded.floor_price,
            cap_price = excluded.cap_price, last_price = excluded.last_price,
            cogs = excluded.cogs, cogs_source = excluded.cogs_source,
            last_action_id = excluded.last_action_id,
            last_step_on = excluded.last_step_on, updated_at = now()""",
        (account, p["offer_id"], p["rung"], p["floor"], p["cap"], p["price"],
         p["cogs"], p["cogs_src"], aid, today))


def _log(account, aid, title, p, ok, note):
    db.execute("""insert into oz_action_log (account, action_id, action_title, offer_id,
                      product_id, action_price, stock, rung, ok, note)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
               (account, aid, title, p.get("offer_id"), p["product_id"], p.get("price"),
                p["stock"], p.get("rung"), ok, note))


def apply_plan(account, plans, dry=True, allow_remove=False):
    """add/update — через activate (тот же метод переустанавливает цену), remove — deactivate.

    Снятие с акции трогает то, что человек уже завёл руками, поэтому требует allow_remove.
    """
    done = {"add": 0, "update": 0, "remove": 0}
    today = dt.date.today()
    for aid in sorted({p["action_id"] for p in plans}):
        part = [p for p in plans if p["action_id"] == aid]
        title = part[0]["action_title"]
        put = [p for p in part if p["mode"] in ("add", "update")]
        rm = [p for p in part if p["mode"] == "remove"] if allow_remove else []
        if dry:
            for m in ("add", "update", "remove"):
                n = sum(1 for p in part if p["mode"] == m)
                if n:
                    print(f"  [dry] {title}: {m} {n} шт", flush=True)
            continue
        # товар уже стоит по нужной цене — на площадку не ходим, но ступень помним,
        # иначе недельный отсчёт у него не начнётся и лестница не тронется
        for p in part:
            if p["mode"] == "keep":
                _remember(account, aid, p, today)
        if put:
            res = _req(account, "POST", "/v1/actions/products/activate",
                       {"action_id": aid,
                        "products": [{"product_id": p["product_id"], "action_price": p["price"],
                                      "stock": max(p["stock"], p["min_stock"], 1)} for p in put]}
                       ).get("result", {})
            ok_ids = {int(x) for x in (res.get("product_ids") or [])}
            rej = {int(r.get("product_id")): r.get("reason") for r in (res.get("rejected") or [])}
            for p in put:
                good = p["product_id"] in ok_ids
                _log(account, aid, title, p, good, None if good else rej.get(p["product_id"], "отказ"))
                if good:
                    done[p["mode"]] += 1
                    _remember(account, aid, p, today)
            print(f"  {title}: принято {len(ok_ids)}, отказ {len(rej)}", flush=True)
        if rm:
            res = _req(account, "POST", "/v1/actions/products/deactivate",
                       {"action_id": aid, "product_ids": [p["product_id"] for p in rm]}
                       ).get("result", {})
            ok_ids = {int(x) for x in (res.get("product_ids") or [])}
            for p in rm:
                good = p["product_id"] in ok_ids
                _log(account, aid, title, p, good, "снят: цена ниже порога" if good else "снять не вышло")
                done["remove"] += int(good)
            print(f"  {title}: снято {len(ok_ids)} из {len(rm)}", flush=True)
    return done


# --- сторож акций и бот -----------------------------------------------------
def send(text):
    token = os.getenv("DROPBOX_BOT_TOKEN", "")
    if not token:
        print("нет DROPBOX_BOT_TOKEN — отправка пропущена", flush=True)
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": SERGEY_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                      timeout=30)
    ok = r.status_code == 200 and r.json().get("ok")
    print(f"телеграм: {'отправлено' if ok else 'ОШИБКА ' + r.text[:200]}", flush=True)
    return ok


def _dt(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def watch(account, notify=False):
    """Реестр акций: в бот — только напоминание за сутки до старта распродажи стока."""
    seen = {r["action_id"]: r for r in db.query(
        "select * from oz_action_seen where account = %s", (account,))}
    msgs, recs = [], []
    now = dt.datetime.now(dt.timezone.utc)
    for a in actions(account):
        aid, title = a.get("id"), a.get("title")
        wl = is_whitelisted(title)
        ds, de = _dt(a.get("date_start")), _dt(a.get("date_end"))
        fz = _dt(a.get("freeze_date"))
        old = seen.get(aid)
        new_n = bool(old and old["notified_new"])
        start_n = bool(old and old["notified_start"])
        if wl and not old:
            new_n = True          # в бот идёт ТОЛЬКО предупреждение о старте (решение Сергея 22.08)
        if wl and ds and not start_n and dt.timedelta(0) <= (ds - now) <= dt.timedelta(days=1):
            msgs.append(f"⏰ *Завтра старт*: {title}\n"
                        f"начало {ds:%d.%m %H:%M}" + (f", заморозка цен {fz:%d.%m %H:%M}" if fz else ""))
            start_n = True
        recs.append({"account": account, "action_id": aid, "title": title,
                     "date_start": ds, "date_end": de, "freeze_date": fz, "whitelisted": wl,
                     "notified_new": new_n, "notified_start": start_n,
                     "updated_at": dt.datetime.now(dt.timezone.utc)})
    if recs:
        db.upsert("oz_action_seen", recs, conflict_cols=["account", "action_id"],
                  update_cols=["title", "date_start", "date_end", "freeze_date", "whitelisted",
                               "notified_new", "notified_start", "updated_at"])
    for m in msgs:
        print(m.replace("*", ""), flush=True)
        if notify:
            send(f"*{account}*\n{m}")
    if not msgs:
        print(f"  {account}: новых акций и стартов в ближайшие сутки нет", flush=True)
    return msgs


def report(account, plans, keep, keep_src, done, dry):
    m = {}
    for p in plans:
        m[p["mode"] or "мимо"] = m.get(p["mode"] or "мимо", 0) + 1
    ru = {"add": "завести", "update": "сдвинуть ступень", "keep": "оставить как есть",
          "remove": "снять", "мимо": "не подходят"}   # снимаем по нулю на складе ИЛИ по цене
    lines = [f"*{account}* · акции по белому списку · {'расчёт' if dry else 'исполнение'}",
             f"нам остаётся {keep*100:.1f}% выручки ({keep_src}), цель +{GOAL_NET:.0f} ₽ с единицы"]
    for k in ("add", "update", "keep", "remove", "мимо"):
        if m.get(k):
            lines.append(f"  · {ru[k]}: {m[k]}")
    why = {}
    for p in plans:
        if p.get("skip"):
            k = re.sub(r"\d+", "N", p["skip"])
            why[k] = why.get(k, 0) + 1
    for k, n in sorted(why.items(), key=lambda x: -x[1])[:4]:
        lines.append(f"  – {k}: {n}")
    if not dry:
        lines.append("сделано: " + ", ".join(f"{ru[k]} {v}" for k, v in done.items() if v) or "изменений нет")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Ozon: автоучастие в акциях белого списка (распродажа стока + региональные)")
    ap.add_argument("cmd", choices=["plan", "apply", "watch"])
    ap.add_argument("--account", choices=ACCOUNTS, help="по умолчанию оба")
    ap.add_argument("--notify", action="store_true", help="слать в бот")
    ap.add_argument("--csv", help="файл с построчным расчётом")
    ap.add_argument("--allow-remove", action="store_true",
                    help="разрешить снятие с акции того, что уже заведено (цена ниже порога)")
    a = ap.parse_args()
    accs = [a.account] if a.account else ACCOUNTS
    rows = ["account;action;offer_id;название;потолок;пол;себест;источник;остаток;мин;цена;ступень;решение"]
    for acc in accs:
        print(f"\n=== {acc} ===", flush=True)
        if a.cmd == "watch":
            watch(acc, a.notify)
            continue
        plans, keep, keep_src, acts = plan_account(acc)
        if not acts:
            print("  белых акций нет", flush=True)
            continue
        done = apply_plan(acc, plans, dry=(a.cmd == "plan"), allow_remove=a.allow_remove)
        for p in plans:
            rows.append(";".join(str(x) for x in [
                acc, p["action_title"], p.get("offer_id"), p.get("name"), f"{p['cap']:.0f}",
                f"{p.get('floor') or 0:.0f}", f"{p.get('cogs') or 0:.0f}", p.get("cogs_src", ""),
                p["stock"], p["min_stock"], p.get("now_price") or "", p.get("price") or "",
                p.get("rung", ""), p["mode"] or "", p.get("skip") or p.get("why", "")]))
        txt = report(acc, plans, keep, keep_src, done, a.cmd == "plan")
        print(txt.replace("*", ""), flush=True)
        if a.notify:
            send(txt)
    if a.csv and len(rows) > 1:
        pathlib.Path(a.csv).write_text("\n".join(rows), encoding="utf-8")
        print(f"\nпострочно → {a.csv}", flush=True)


if __name__ == "__main__":
    main()

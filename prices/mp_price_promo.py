# поток: prc
"""prices/mp_price_promo.py — ежедневный срез «цена + участие в акции» → prc_mp_price_day.

Зачем: площадки отдают в отчётах цену УЖЕ со скидкой, а величина скидки продавца и флаг
акции в выгрузках пустые. Поэтому задним числом нельзя отделить эффект акции от сезона.
Копим историю сами: по одной строке в день на карточку, обе фирмы, три площадки.
Когда накопятся входы и выходы, эффект акции считается сравнением «до/после» с карточками
без акции как контролем.

Состав: только карточки с продажами за 90 дней (решение Сергея 23.09.2026) — вчетверо
меньше строк, новинки подхватятся после первых продаж.

Источники (бесплатные, только чтение):
- WB     цены  `discounts-prices-api /api/v2/list/goods/filter` (нужен скоуп «Цены и скидки»),
         акции `dp-calendar-api /api/v1/calendar/promotions` + `/promotions/nomenclatures`,
         цена покупателя (с СПП) — публичная карточка `card.wb.ru/cards/v4/detail`.
- Ozon   цены  `/v5/product/info/prices` (`marketing_price` = цена покупателя),
         акции `/v1/actions` (наши) + `/v1/actions/products`.
- Маркет акции `/businesses/{id}/promos` + `/promos/offers`; цены Маркета тут не снимаем —
         они уже есть в витрине цен потока prc.

Запуск:  ./venv/bin/python -m prices.mp_price_promo [--day YYYY-MM-DD] [--platform wb|ozon|yandex]
"""
import os
import sys
import time
import argparse
import pathlib
import datetime as dt
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors.ozon import _headers as _oz_headers  # noqa: E402

load_dotenv(BASE_DIR / ".env")

SOLD_DAYS = 90
WB_GOODS = "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter"
WB_PROMOS = "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions"
WB_PROMO_NMS = WB_PROMOS + "/nomenclatures"
WB_CARD = "https://card.wb.ru/cards/v4/detail"
OZ_PRICES = "https://api-seller.ozon.ru/v5/product/info/prices"
OZ_ACTIONS = "https://api-seller.ozon.ru/v1/actions"
OZ_ACTION_PRODUCTS = "https://api-seller.ozon.ru/v1/actions/products"
YA_BASE = "https://api.partner.market.yandex.ru"
WB_TOKEN = {"wb_acc1": "WB_TOKEN_PRICES_ACC1", "wb_acc2": "WB_TOKEN_PRICES_ACC2"}
YA_ENV = {"ya_acc1": ("YANDEX_API_KEY_ACC1", "YANDEX_BUSINESS_ID_ACC1")}


def _get(url, **kw):
    """GET/POST с терпением к 429: площадки отвечают лимитом, а не ошибкой."""
    method = kw.pop("method", "get")
    for _ in range(4):
        r = requests.request(method, url, timeout=90, **kw)
        if r.status_code != 429:
            return r
        time.sleep(int(r.headers.get("Retry-After", "10")) + 3)
    return r


# ---------------------------------------------------------------- отбор карточек
def sold_items(platform, account, day):
    """Карточки с продажами за SOLD_DAYS дней. Ключ — тот же, что у API площадки."""
    since = day - dt.timedelta(days=SOLD_DAYS)
    if platform == "wb":
        q = """select distinct payload->>'nm_id' i from raw_wb_report
                where account = %s and (payload->>'rr_dt')::date >= %s
                  and payload->>'doc_type_name' = 'Продажа'"""
    elif platform == "ozon":
        # в транзакциях товар — sku, в API цен и акций — product_id/offer_id: связка через каталог
        q = """select distinct p.offer_id i
                 from raw_ozon_transaction t
                 cross join lateral jsonb_array_elements(coalesce(t.payload->'items', '[]'::jsonb)) it
                 join ozon_product p on p.account = t.account and p.sku::text = it->>'sku'
                where t.account = %s and (t.payload->>'operation_date')::date >= %s"""
    else:
        # у Маркета товар лежит в items[], а дата заказа приходит в формате ДД-ММ-ГГГГ —
        # фильтруем по периоду выгрузки, он уже DATE
        q = """select distinct it->>'offerId' i
                 from raw_yandex_order o
                 cross join lateral jsonb_array_elements(coalesce(o.payload->'items', '[]'::jsonb)) it
                where o.account = %s and o.period_to >= %s"""
    return {r["i"] for r in db.query(q, (account, since)) if r["i"]}


# ---------------------------------------------------------------- WB
def wb_snapshot(account, items, day):
    token = os.getenv(WB_TOKEN[account])
    if not token:
        return [], f"нет токена {WB_TOKEN[account]}"
    h = {"Authorization": token}
    goods, offset = {}, 0
    while True:
        r = _get(WB_GOODS, headers=h, params={"limit": 1000, "offset": offset})
        if r.status_code != 200:
            return [], f"goods/filter {r.status_code}: {r.text[:100]}"
        batch = (r.json().get("data") or {}).get("listGoods") or []
        for g in batch:
            nm = str(g.get("nmID"))
            if nm in items:
                s = (g.get("sizes") or [{}])[0]
                goods[nm] = {"vendor_code": g.get("vendorCode"),
                             "price_list": s.get("price"),
                             "price_sale": s.get("discountedPrice"),
                             "discount_pct": g.get("discount")}
        if len(batch) < 1000:
            break
        offset += 1000
        time.sleep(0.3)

    # Участие в акции — из wb_promo_plan_price (та же таблица, по которой работает сторож
    # ops/wb_promo_guard.py). Календарный API отдаёт 422 по чужим акциям и жёстко лимитирует,
    # а плановые цены приходят выгрузкой из кабинета и содержат нужный флаг и % скидки.
    promos, plan = {}, {}
    for r in db.query("""select distinct on (nm_id, promo_name) nm_id, promo_name, in_promo, disc_pct
                           from wb_promo_plan_price
                          where account = %s and in_promo
                            and coalesce(valid_to, %s) >= %s
                          order by nm_id, promo_name, loaded_at desc""", (account, day, day)):
        promos.setdefault(str(r["nm_id"]), []).append(r["promo_name"])
        plan[str(r["nm_id"])] = r["disc_pct"]
    active = {n for ns in promos.values() for n in ns}

    buyer = {}           # цена покупателя с СПП — публичная карточка, по 100 штук
    nms = sorted(goods)
    for i in range(0, len(nms), 100):
        chunk = nms[i:i + 100]
        rc = _get(WB_CARD, params={"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30,
                                   "nm": ";".join(chunk)})
        if rc.status_code == 200:
            for p in (rc.json().get("products") or []):
                pr = ((p.get("sizes") or [{}])[0].get("price") or {})
                buyer[str(p.get("id"))] = {"price_buyer": (pr.get("product") or 0) / 100 or None,
                                           "stock": p.get("totalQuantity")}
        time.sleep(0.4)

    rows = []
    for nm, g in goods.items():
        b = buyer.get(nm, {})
        rows.append({"platform": "wb", "account": account, "item_id": nm, "day": day,
                     "vendor_code": g["vendor_code"], "price_list": g["price_list"],
                     "price_sale": g["price_sale"], "price_buyer": b.get("price_buyer"),
                     "discount_pct": g["discount_pct"], "in_promo": nm in promos,
                     "promo_names": "; ".join(promos.get(nm, [])) or None,
                     "stock": b.get("stock"), "source": "wb goods/filter+calendar+card"})
    return rows, f"акций {len(active)}, карточек в акции {len(promos)}"


# ---------------------------------------------------------------- Ozon
def ozon_snapshot(account, items, day):
    h = _oz_headers(account)
    sku_by_offer = {r["offer_id"]: str(r["sku"]) for r in db.query(
        "select offer_id, sku from ozon_product where account = %s", (account,))}
    prices, cursor = {}, ""
    while True:
        r = _get(OZ_PRICES, method="post", headers=h,
                 json={"cursor": cursor, "filter": {"visibility": "ALL"}, "limit": 1000})
        if r.status_code != 200:
            return [], f"info/prices {r.status_code}: {r.text[:100]}"
        d = r.json()
        for it in d.get("items") or []:
            offer = str(it.get("offer_id"))
            p = it.get("price") or {}
            if offer in items:
                sku = sku_by_offer.get(offer) or str(it.get("product_id"))
                prices[sku] = {"product_id": str(it.get("product_id")),
                               "vendor_code": it.get("offer_id"),
                               "price_list": _num(p.get("old_price")),
                               "price_sale": _num(p.get("price")),
                               "price_buyer": _num(p.get("marketing_price")) or _num(p.get("price"))}
        cursor = d.get("cursor") or ""
        if not cursor or not (d.get("items") or []):
            break
        time.sleep(0.3)

    promos = defaultdict(list)
    ra = _get(OZ_ACTIONS, headers=h)
    acts = [a for a in (ra.json().get("result") or []) if a.get("is_participating")] \
        if ra.status_code == 200 else []
    for a in acts:
        last_id, guard = "", 0
        while guard < 50:      # last_id — СТРОКА, на первой странице не передаём (число → 400)
            guard += 1
            body = {"action_id": a["id"], "limit": 1000}
            if last_id:
                body["last_id"] = last_id
            rp = _get(OZ_ACTION_PRODUCTS, method="post", headers=h, json=body)
            if rp.status_code != 200:
                break
            res = rp.json().get("result") or {}
            for p in res.get("products") or []:
                promos[str(p.get("id"))].append(a["title"])
            nxt = str(res.get("last_id") or "")
            if nxt in ("", "0") or nxt == last_id:
                break
            last_id = nxt
            time.sleep(0.3)

    rows = []
    for sku, g in prices.items():
        rows.append({"platform": "ozon", "account": account, "item_id": sku, "day": day,
                     "vendor_code": g["vendor_code"], "price_list": g["price_list"],
                     "price_sale": g["price_sale"], "price_buyer": g["price_buyer"],
                     "discount_pct": _disc(g["price_list"], g["price_sale"]),
                     "in_promo": g["product_id"] in promos,
                     "promo_names": "; ".join(promos.get(g["product_id"], [])) or None,
                     "stock": None, "source": "ozon info/prices+actions"})
    return rows, f"наших акций {len(acts)}, карточек в акции {len(promos)}"


# ---------------------------------------------------------------- Маркет
def yandex_snapshot(account, items, day):
    key_env, biz_env = YA_ENV[account]
    h = {"Api-Key": os.getenv(key_env)}
    biz = os.getenv(biz_env)
    r = _get(f"{YA_BASE}/businesses/{biz}/promos", method="post", headers=h, json={})
    if r.status_code != 200:
        return [], f"promos {r.status_code}: {r.text[:100]}"
    promos = defaultdict(list)
    plist = ((r.json().get("result") or {}).get("promos") or [])
    for p in plist:
        page = None
        while True:
            body = {"promoId": p["id"]}
            params = {"limit": 500, **({"page_token": page} if page else {})}
            rp = _get(f"{YA_BASE}/businesses/{biz}/promos/offers", method="post", headers=h,
                      json=body, params=params)
            if rp.status_code != 200:
                break
            res = rp.json().get("result") or {}
            for o in res.get("offers") or []:
                promos[str(o.get("offerId"))].append(p.get("name") or p["id"])
            page = (res.get("paging") or {}).get("nextPageToken")
            if not page:
                break
            time.sleep(0.3)
    rows = [{"platform": "yandex", "account": account, "item_id": i, "day": day,
             "vendor_code": i, "price_list": None, "price_sale": None, "price_buyer": None,
             "discount_pct": None, "in_promo": i in promos,
             "promo_names": "; ".join(promos.get(i, [])) or None, "stock": None,
             "source": "yandex promos/offers"}
            for i in sorted(items)]
    return rows, f"акций {len(plist)}, карточек в акции {len(promos)}"


def _num(v):
    try:
        return round(float(v), 2) or None
    except (TypeError, ValueError):
        return None


def _disc(old, now):
    return round((1 - now / old) * 100, 2) if old and now and old > 0 else None


SNAPSHOT = {"wb": wb_snapshot, "ozon": ozon_snapshot, "yandex": yandex_snapshot}
ACCOUNTS = {"wb": ["wb_acc1", "wb_acc2"], "ozon": ["oz_acc1", "oz_acc2"], "yandex": ["ya_acc1"]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="")
    ap.add_argument("--platform", default="")
    a = ap.parse_args(argv)
    day = dt.date.fromisoformat(a.day) if a.day else dt.date.today()
    plats = [a.platform] if a.platform else list(SNAPSHOT)
    total = 0
    for platform in plats:
        for account in ACCOUNTS[platform]:
            items = sold_items(platform, account, day)
            if not items:
                print(f"  {platform}/{account}: продаж за {SOLD_DAYS} дней нет", flush=True)
                continue
            try:
                rows, note = SNAPSHOT[platform](account, items, day)
            except Exception as e:      # одна площадка молчит — остальные всё равно снимутся
                print(f"  [FAIL] {platform}/{account}: {type(e).__name__}: {str(e)[:120]}", flush=True)
                continue
            if rows:
                db.upsert("prc_mp_price_day", rows,
                          conflict_cols=["platform", "account", "item_id", "day"])
                total += len(rows)
            inp = sum(1 for r in rows if r["in_promo"])
            print(f"  {platform}/{account}: продавалось {len(items)}, снято {len(rows)}, "
                  f"в акции {inp} — {note}", flush=True)
    print(f"Срез цен и акций {day:%d.%m.%Y}: строк {total}", flush=True)


if __name__ == "__main__":
    main()

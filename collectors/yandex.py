"""collectors/yandex.py — заказы Яндекс.Маркет (Partner API, бизнес-уровень).

POST /v1/businesses/{businessId}/orders — все заказы кабинета по всем магазинам (campaignId),
пагинация page_token. Возвращает ≈последние 30 дней. Деньги: order.prices.payment.value
(заплатил покупатель), .subsidy.value (доплата Маркета). Кладём по заказу в raw_yandex_order.

Авторизация: header Api-Key (YANDEX_API_KEY_ACC1), businessId (YANDEX_BUSINESS_ID_ACC1).

Запуск:  ./venv/bin/python collectors/yandex.py
"""
import os
import sys
import datetime
import pathlib

import requests
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"


def _cfg():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    biz = os.getenv("YANDEX_BUSINESS_ID_ACC1")
    if not key or not biz:
        raise RuntimeError("YANDEX_API_KEY_ACC1 / YANDEX_BUSINESS_ID_ACC1 не заданы в .env")
    return key, biz


def fetch_orders():
    key, biz = _cfg()
    H = {"Api-Key": key, "Content-Type": "application/json"}
    out, tok = [], None
    for _ in range(200):
        params = {"limit": 50}
        if tok:
            params["page_token"] = tok
        r = requests.post(f"{API}/v1/businesses/{biz}/orders", headers=H, params=params,
                          json={}, timeout=90)
        r.raise_for_status()
        j = r.json()
        out += j.get("orders", [])
        tok = (j.get("paging") or {}).get("nextPageToken")
        if not tok:
            break
    return out


def _rev(o):
    return float(((o.get("prices") or {}).get("payment") or {}).get("value") or 0)


def build_cogs(days=30):
    """Себестоимость по offerId из МС-заказов «Покупатель Маркет»/«Я.Маркет Экспресс»
    (Σ cost_seb реальных позиций ÷ qty по external_code). Источник — report/stock приёмок, как
    у ВБ/Озон; НЕ buy_price и НЕ карточка (там нулевые варианты). 100% покрытие проданного."""
    tok = os.getenv("MOYSKLAD_TOKEN")
    H = {"Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip"}
    MS = "https://api.moysklad.ru/api/remap/1.2"
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    cost = {r["ms_id"]: float(r["cost_seb"] or 0) for r in db.query("SELECT ms_id, cost_seb FROM products")}
    ec = {r["ms_id"]: r["external_code"] for r in db.query(
        "SELECT ms_id, external_code FROM products WHERE external_code IS NOT NULL")}
    agg = {}   # external_code -> [sum_cost, sum_qty]
    for name in ("Покупатель Маркет", "Я.Маркет Экспресс"):
        rr = requests.get(f"{MS}/entity/counterparty", headers=H,
                          params={"filter": f"name={name}"}, timeout=60).json().get("rows", [])
        if not rr:
            continue
        href = rr[0]["meta"]["href"]
        offset = 0
        while True:
            r = requests.get(f"{MS}/entity/customerorder", headers=H, timeout=90, params={
                "filter": f"agent={href};moment>={since}", "limit": 100, "offset": offset,
                "expand": "positions.assortment"})
            rows = r.json().get("rows", [])
            if not rows:
                break
            for o in rows:
                for p in (o.get("positions") or {}).get("rows", []):
                    a = p.get("assortment") or {}
                    msid = a.get("id") or a.get("meta", {}).get("href", "").split("/")[-1].split("?")[0]
                    code = ec.get(msid)
                    if not code:
                        continue
                    q = p.get("quantity", 0) or 0
                    s = agg.setdefault(code, [0.0, 0.0])
                    s[0] += cost.get(msid, 0.0) * q
                    s[1] += q
            offset += 100
            if len(rows) < 100:
                break
    recs = [{"offer": code, "cost_per_unit": round(c / q, 2)} for code, (c, q) in agg.items() if q]
    total_cogs = sum(c for c, q in agg.values())
    recs.append({"offer": "__total__", "cost_per_unit": round(total_cogs, 2)})   # итог COGS из МС
    n = db.upsert("yandex_cost", recs, conflict_cols=["offer"], update_cols=["cost_per_unit"])
    print(f"Себестоимость Маркета: {n-1} offer'ов + итог {total_cogs:,.0f} ₽ (из МС-заказов)", flush=True)


def build_finance(days=30):
    """Реальные расходы Маркета из stats/orders: комиссия (FEE), логистика (DELIVERY_*),
    эквайринг (PAYMENT_TRANSFER), продвижение (AUCTION_PROMOTION) и пр. — order.commissions[].actual.
    Заполняются после доставки/выплаты (свежие заказы пустые — как лаг отчёта). Кладём итог в
    yandex_cost['__mp_cost__'] → настоящая чистая = выручка − COGS − расходы Маркета."""
    key, biz = _cfg()
    H = {"Api-Key": key, "Content-Type": "application/json"}
    camps = [c.strip() for c in (os.getenv("YANDEX_CAMPAIGN_ID_ACC1") or "").split(",") if c.strip()]
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    today = datetime.date.today().isoformat()
    from collections import Counter
    by_type = Counter()
    for cid in camps:
        tok = None
        for _ in range(60):
            params = {"page_token": tok} if tok else {}
            r = requests.post(f"{API}/campaigns/{cid}/stats/orders", headers=H, params=params,
                              json={"dateFrom": since, "dateTo": today}, timeout=90)
            if r.status_code != 200:
                break
            res = r.json().get("result", {})
            for o in res.get("orders", []):
                for c in (o.get("commissions") or []):
                    by_type[c.get("type")] += c.get("actual", 0) or 0
            tok = (res.get("paging") or {}).get("nextPageToken")
            if not tok:
                break
    total = round(sum(by_type.values()))
    db.upsert("yandex_cost", [{"offer": "__mp_cost__", "cost_per_unit": total}],
              conflict_cols=["offer"], update_cols=["cost_per_unit"])
    print(f"Расходы Маркета: {total:,} ₽ (комиссия+логистика+эквайринг+реклама) | "
          f"по типам: {dict(by_type)}", flush=True)


def main():
    print("Яндекс.Маркет: заказы (бизнес-уровень)", flush=True)
    orders = fetch_orders()
    today = datetime.date.today().isoformat()
    pf = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    recs = []
    for o in orders:
        oid = o.get("orderId")
        if oid is None:
            continue
        recs.append({"account": ACCOUNT, "order_id": str(oid), "item_id": "0",
                     "period_from": pf, "period_to": today,
                     "payload": psycopg2.extras.Json(o)})
    n = db.upsert("raw_yandex_order", recs, conflict_cols=["account", "order_id", "item_id"],
                  update_cols=["period_from", "period_to", "payload"])
    rev = sum(_rev(o) for o in orders)
    print(f"Записано заказов: {n} | выручка(payment) {rev:,.0f} ₽ | "
          f"магазинов {len({o.get('campaignId') for o in orders})}", flush=True)
    build_cogs()
    build_finance()


if __name__ == "__main__":
    main()

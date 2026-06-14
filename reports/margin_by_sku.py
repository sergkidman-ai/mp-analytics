"""reports/margin_by_sku.py — Этап 3. Витрина маржи: WB-деньги − реальный COGS из МС.

Связка (раздел 10 MOYSKLAD_RECON.md): WB `assembly_id` = МС `name` заказа.
COGS WB-продажи = Σ buy_price реально отгруженных компонентов из МС (по ms_id) —
корректно для НАБОРОВ (WB 1 юнит = N компонентов в МС).

net_profit = to_pay − logistics − storage − acceptance − other − COGS
(to_pay = Σ ppvz_for_pay уже нетто возвратов; деньги — якорь сверки, не штуки).

Запуск:  ./venv/bin/python reports/margin_by_sku.py
"""
import os
import sys
import time
import pathlib
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS_TOK = os.getenv("MOYSKLAD_TOKEN")
MS = "https://api.moysklad.ru/api/remap/1.2"
H = {"Authorization": f"Bearer {MS_TOK}", "Accept-Encoding": "gzip",
     "Accept": "application/json;charset=utf-8"}


def _ms(path, params=None):
    r = requests.get(f"{MS}/{path}", headers=H, params=params, timeout=60)
    time.sleep(0.25)
    return r.json()


def _href_id(href):
    return href.rstrip("/").split("/")[-1]


def demand_cogs_by_order(date_from, date_to, ms_agent="Покупатель ВБ",
                         ms_org='ООО "ЦИФРОВОЙ КВАДРАТ"'):
    """Себестоимость каждого WB-заказа = Σ buy_price компонентов отгрузки МС.

    Возвращает {order_name(assembly_id): cogs_rub}. Компоненты резолвятся по ms_id
    позиции отгрузки → products.buy_price (точный COGS, в т.ч. наборы)."""
    prod = {r["ms_id"]: float(r["buy_price"] or 0)
            for r in db.query("SELECT ms_id, buy_price FROM products")}
    ag = _ms("entity/counterparty", {"filter": f"name={ms_agent}", "limit": 1})["rows"][0]["meta"]["href"]
    org = _ms("entity/organization", {"filter": f"name={ms_org}", "limit": 1})["rows"][0]["meta"]["href"]
    flt = (f"agent={ag};organization={org};"
           f"moment>={date_from} 00:00:00;moment<={date_to} 23:59:59")
    out, offset = {}, 0
    while True:
        j = _ms("entity/demand", {"limit": 100, "offset": offset,
                                  "filter": flt, "expand": "positions.assortment"})
        rows = j.get("rows", [])
        for d in rows:
            cogs = 0.0
            for p in d.get("positions", {}).get("rows", []):
                ms_id = _href_id(p.get("assortment", {}).get("meta", {}).get("href", ""))
                cogs += prod.get(ms_id, 0.0) * (p.get("quantity", 0) or 0)
            out[d.get("name")] = cogs
        offset += 100
        if not rows or offset >= j.get("meta", {}).get("size", 0):
            break
    return out


def build(account="wb_acc1", date_from="2026-05-01", date_to="2026-05-31"):
    # 1) COGS по заказам из МС
    print("Считаю COGS заказов из МС…", flush=True)
    cogs_order = demand_cogs_by_order(date_from, date_to)
    print(f"  заказов МС с COGS: {len(cogs_order)}", flush=True)

    # 2) assembly_id → nm_id из сырья WB (по строкам Продажа)
    asm = db.query("""SELECT DISTINCT payload->>'assembly_id' a, payload->>'nm_id' nm
                      FROM raw_wb_report WHERE account=%s
                        AND payload->>'supplier_oper_name'='Продажа'
                        AND coalesce(payload->>'assembly_id','')<>''""", (account,))
    cogs_nm = defaultdict(float)
    matched = unmatched = 0
    for r in asm:
        if r["a"] in cogs_order:
            cogs_nm[r["nm"]] += cogs_order[r["a"]]
            matched += 1
        else:
            unmatched += 1

    # 3) деньги по nm_id из sales + COGS → margin_by_sku
    sales = db.query("""SELECT * FROM sales WHERE platform='wb' AND account=%s
                        AND period_from=%s AND period_to=%s""", (account, date_from, date_to))
    recs = []
    for s in sales:
        rev = float(s["revenue_buyer"] or 0)
        cogs = cogs_nm.get(s["article"], 0.0)
        to_pay = float(s["to_pay"] or 0)
        net = to_pay - float(s["logistics"] or 0) - float(s["storage"] or 0) \
            - float(s["acceptance"] or 0) - float(s["other"] or 0) - cogs
        recs.append({
            "article": s["article"], "platform": "wb", "account": account,
            "period_from": date_from, "period_to": date_to,
            "qty": s["qty"], "revenue_buyer": rev, "cogs": cogs,
            "commission": s["commission"], "logistics": s["logistics"],
            "returns_sum": s["returns_sum"], "storage": s["storage"],
            "acceptance": s["acceptance"], "other": s["other"],
            "net_profit": net, "margin_pct": (net / rev * 100) if rev else None,
            "commission_pct": (float(s["commission"] or 0) / rev * 100) if rev else None,
        })
    db.upsert("margin_by_sku", recs, conflict_cols=[
        "article", "platform", "account", "period_from", "period_to"])

    cov = matched / (matched + unmatched) * 100 if (matched + unmatched) else 0
    print(f"  WB-продаж (assembly): matched COGS {matched}, без COGS {unmatched} "
          f"(покрытие {cov:.0f}%)", flush=True)
    print(f"  записано в margin_by_sku: {len(recs)} nm_id", flush=True)
    return account, date_from, date_to


if __name__ == "__main__":
    build()

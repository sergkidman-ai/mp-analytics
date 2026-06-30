"""collectors/suppliers.py — ежедневный снимок остатков + поставщиков из МойСклада.

report/stock/all → остаток, в пути, резерв, stock_days (дни запаса, МС считает сам), себест.
entity/product (expand=supplier) → поставщик + закупочная цена (buyPrice).
Складываем в supplier_stock (снимок на дату). Основа дашборда дефицита/арбитража.

Запуск:  ./venv/bin/python collectors/suppliers.py
"""
import os
import sys
import time
import pathlib
import datetime

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS = "https://api.moysklad.ru/api/remap/1.2"
H = {"Authorization": f"Bearer {os.getenv('MOYSKLAD_TOKEN')}", "Accept-Encoding": "gzip"}


def _ms(path, **params):
    r = requests.get(f"{MS}/{path}", headers=H, params=params, timeout=120)
    time.sleep(0.2)
    r.raise_for_status()
    return r.json()


def supplier_map():
    """{ms_id: (supplier_name, buy_price)} — по всем товарам (expand=supplier)."""
    out, off = {}, 0
    while True:
        j = _ms("entity/product", expand="supplier", limit=100, offset=off)
        rows = j.get("rows", [])
        for p in rows:
            sup = (p.get("supplier") or {}).get("name")
            bp = (p.get("buyPrice") or {}).get("value")
            pid = p.get("meta", {}).get("href", "").split("/")[-1].split("?")[0] or p.get("id")
            out[pid] = (sup, (bp / 100 if bp else None))
        off += len(rows)
        if off >= j.get("meta", {}).get("size", 0) or not rows:
            break
        if off % 5000 == 0:
            print(f"  [supplier_map] {off}", flush=True)
    print(f"  поставщики по {len(out)} товарам", flush=True)
    return out


def fetch_stock_store(store_href):
    """Остатки на конкретном складе (filter=store)."""
    out, off = [], 0
    while True:
        j = _ms("report/stock/all", limit=1000, offset=off, filter=f"store={store_href}")
        rows = j.get("rows", [])
        out.extend(rows)
        off += len(rows)
        if off >= j.get("meta", {}).get("size", 0) or not rows:
            break
    return out


def fetch_turnover(days=30):
    """{ms_id: продано за N дней} — оборачиваемость (расход) для прогноза дефицита."""
    to = datetime.date.today()
    fr = to - datetime.timedelta(days=days)
    out, off = {}, 0
    while True:
        j = _ms("report/turnover/all", momentFrom=f"{fr} 00:00:00",
                momentTo=f"{to} 00:00:00", limit=1000, offset=off)
        rows = j.get("rows", [])
        for r in rows:
            mid = r.get("assortment", {}).get("meta", {}).get("href", "").split("/")[-1].split("?")[0]
            q = (r.get("outcome") or {}).get("quantity") or 0
            if mid and q:
                out[mid] = out.get(mid, 0) + q
        off += len(rows)
        if off >= j.get("meta", {}).get("size", 0) or not rows:
            break
    return out


def main(captured=None):
    captured = captured or datetime.date.today().isoformat()
    print(f"Остатки по складам МС на {captured}", flush=True)
    sup = supplier_map()
    turn = fetch_turnover()
    print(f"  оборачиваемость: {len(turn)} товаров с продажами/30д", flush=True)
    stores = {s["name"]: s["meta"]["href"] for s in _ms("entity/store", limit=100).get("rows", [])}
    recs = []
    for sname, shref in stores.items():
        rows = fetch_stock_store(shref)
        cnt = 0
        for r in rows:
            if not (r.get("stock") or 0):
                continue
            ms_id = r.get("meta", {}).get("href", "").split("/")[-1].split("?")[0]
            if not ms_id:
                continue
            s_name, s_bp = sup.get(ms_id, (None, None))
            recs.append({
                "captured_at": captured, "ms_id": ms_id, "store": sname,
                "name": r.get("name"), "article": r.get("article"),
                "external_code": r.get("externalCode"), "supplier": s_name,
                "buy_price": s_bp, "cost_seb": (r.get("price") or 0) / 100,
                "stock": r.get("stock"), "in_transit": r.get("inTransit"),
                "reserve": r.get("reserve"), "stock_days": r.get("stockDays"),
                "sold_30d": turn.get(ms_id),
            })
            cnt += 1
        print(f"  {sname}: {cnt} позиций с остатком", flush=True)
    n = db.upsert("supplier_stock", recs, conflict_cols=["captured_at", "ms_id", "store"])
    print(f"Записано в supplier_stock: {n}", flush=True)


if __name__ == "__main__":
    main()

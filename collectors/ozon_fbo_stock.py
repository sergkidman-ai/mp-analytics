"""collectors/ozon_fbo_stock.py — остатки Ozon FBO по складам и аккаунтам.

POST /v2/analytics/stock_on_warehouses (warehouse_type=ALL) → строки sku × склад с
free_to_sell_amount / reserved_amount / promised_amount. Пагинация offset. Снимок на дату
(captured_at) → ozon_fbo_stock; даёт разрез ФБО по юрлицам (Цифровой/Дисквэр), как ВБ.

Запуск:  ./venv/bin/python collectors/ozon_fbo_stock.py [oz_acc1]
"""
import sys
import time
import datetime
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                       # noqa: E402
from collectors.ozon import _headers      # noqa: E402

URL = "https://api-seller.ozon.ru/v2/analytics/stock_on_warehouses"


def fetch(account):
    H = _headers(account)
    out, offset, limit = [], 0, 1000
    while True:
        r = requests.post(URL, headers=H,
                          json={"limit": limit, "offset": offset, "warehouse_type": "ALL"}, timeout=90)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        rows = r.json().get("result", {}).get("rows", [])
        out += rows
        if len(rows) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return out


def main(account="oz_acc1"):
    print(f"Ozon FBO остатки {account}", flush=True)
    cap = datetime.date.today().isoformat()
    rows = fetch(account)
    recs = []
    for x in rows:
        sku = x.get("sku")
        wh = x.get("warehouse_name")
        if sku is None or not wh:
            continue
        recs.append({"account": account, "sku": str(sku), "warehouse": wh,
                     "item_code": x.get("item_code"), "item_name": x.get("item_name"),
                     "free_to_sell": x.get("free_to_sell_amount") or 0,
                     "reserved": x.get("reserved_amount") or 0,
                     "promised": x.get("promised_amount") or 0,
                     "captured_at": cap})
    n = db.upsert("ozon_fbo_stock", recs,
                  conflict_cols=["account", "sku", "warehouse", "captured_at"],
                  update_cols=["item_code", "item_name", "free_to_sell", "reserved", "promised"])
    units = sum(r["free_to_sell"] for r in recs)
    print(f"Записано строк: {n} | складов: {len({r['warehouse'] for r in recs})} | "
          f"к продаже: {units}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")

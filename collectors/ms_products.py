"""collectors/ms_products.py — полный справочник товаров МойСклад в БД.

Тянет /entity/product (≈44k), кладёт в ms_product (buy_price = закупочная/прайс поставщика, ₽;
sale_price; article/code/external_code; archived) и ms_barcode (barcode -> ms_id).
buy_price/value в МС в копейках → ÷100. Это основа для себестоимости, поставщиков, дефицита.

Запуск:  ./venv/bin/python collectors/ms_products.py
"""
import os
import sys
import json
import gzip
import time
import urllib.request
import pathlib

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS = "https://api.moysklad.ru/api/remap/1.2"


def _get(path):
    tok = os.getenv("MOYSKLAD_TOKEN")
    req = urllib.request.Request(MS + path, headers={
        "Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip"})
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    d = gzip.decompress(d)
                return json.loads(d)
        except Exception as e:
            if "429" in str(e):
                time.sleep(3); continue
            raise


def main():
    off, n = 0, 0
    prod_recs, bc_recs = [], []
    while True:
        j = _get(f"/entity/product?limit=1000&offset={off}")
        rows = j.get("rows", [])
        for r in rows:
            bp = (r.get("buyPrice") or {}).get("value", 0) / 100
            sp = next((p.get("value", 0) / 100 for p in r.get("salePrices", [])), 0)
            msid = r["id"]
            prod_recs.append({
                "ms_id": msid, "name": r.get("name"), "article": r.get("article"),
                "code": r.get("code"), "external_code": r.get("externalCode"),
                "buy_price": round(bp, 2) or None, "sale_price": round(sp, 2) or None,
                "archived": bool(r.get("archived")),
            })
            for b in r.get("barcodes", []):
                for v in b.values():
                    bc_recs.append({"barcode": str(v).strip(), "ms_id": msid})
        n += len(rows); off += 1000
        if n % 5000 < 1000:
            print(f"  [ms] {n} товаров…", flush=True)
        if len(rows) < 1000:
            break
    db.upsert("ms_product", prod_recs, conflict_cols=["ms_id"],
              update_cols=["name", "article", "code", "external_code", "buy_price", "sale_price", "archived"])
    # дедуп баркодов (PK barcode)
    seen, ded = set(), []
    for b in bc_recs:
        if b["barcode"] and b["barcode"] not in seen:
            seen.add(b["barcode"]); ded.append(b)
    db.upsert("ms_barcode", ded, conflict_cols=["barcode"], update_cols=["ms_id"])
    withbuy = sum(1 for r in prod_recs if r["buy_price"])
    print(f"Записано: {len(prod_recs)} товаров (с закупочной {withbuy}) | баркодов {len(ded)}", flush=True)


if __name__ == "__main__":
    main()

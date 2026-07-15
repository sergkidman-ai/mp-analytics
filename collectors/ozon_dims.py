"""collectors/ozon_dims.py — задекларированные габариты карточек Ozon → ozon_dims.

Ozon берёт логистику по объёмному весу (объём_см3/5000, кг), который считается из ДхШхВ,
введённых нами в карточку. Раздутый короб = переплата (та же болезнь, что на WB). Тянем
сырые габариты из /v4/product/info/attributes (ДхШхВ мм + вес г), приводим к литрам.

Запуск:  ./venv/bin/python collectors/ozon_dims.py [oz_acc1|oz_acc2]
"""
import sys
import time
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                    # noqa: E402
from collectors.ozon import _headers                   # noqa: E402

ATTR_URL = "https://api-seller.ozon.ru/v4/product/info/attributes"
_MM = {"mm": 0.1, "cm": 1.0, "m": 100.0}               # → см


def _to_l(depth, width, height, unit):
    k = _MM.get((unit or "mm").lower(), 0.1)
    try:
        v = (float(depth) * k) * (float(width) * k) * (float(height) * k) / 1000.0  # см³ → л
    except (TypeError, ValueError):
        return None
    return round(v, 3) if 0 < v <= 200 else None


def _to_g(weight, unit):
    if weight is None:
        return None
    u = (unit or "g").lower()
    return round(float(weight) * (1000.0 if u in ("kg", "kilogram") else 1.0), 1)


def fetch(account):
    H = _headers(account)
    out, last = {}, ""
    while True:
        body = {"filter": {"visibility": "ALL"}, "limit": 1000, "last_id": last}
        r = requests.post(ATTR_URL, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        j = r.json()
        items = j.get("result") or []
        for it in items:
            sku = it.get("sku")
            if not sku:
                continue
            out[str(sku)] = {
                "account": account, "sku": str(sku),
                "offer_id": it.get("offer_id"), "barcode": it.get("barcode"),
                "product_id": it.get("id"),
                "depth_mm": it.get("depth"), "width_mm": it.get("width"),
                "height_mm": it.get("height"), "weight_g": _to_g(it.get("weight"), it.get("weight_unit")),
                "volume_l": _to_l(it.get("depth"), it.get("width"), it.get("height"), it.get("dimension_unit")),
                "name": it.get("name"),
            }
        last = j.get("last_id") or ""
        if not last or len(items) < 1000:
            break
        time.sleep(0.3)
    return list(out.values())


def main(account="oz_acc1"):
    print(f"Ozon габариты {account}", flush=True)
    recs = fetch(account)
    n = db.upsert("ozon_dims", recs, conflict_cols=["account", "sku"],
                  update_cols=["offer_id", "barcode", "product_id", "depth_mm", "width_mm",
                               "height_mm", "weight_g", "volume_l", "name"])
    withvol = sum(1 for r in recs if r["volume_l"])
    print(f"  габаритов записано: {n} | с объёмом: {withvol}", flush=True)
    return n


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        for a in ("oz_acc1", "oz_acc2"):
            main(a)

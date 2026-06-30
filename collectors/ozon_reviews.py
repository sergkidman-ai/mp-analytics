"""collectors/ozon_reviews.py — звёздный рейтинг товаров Ozon из отзывов (Премиум).

POST /v1/review/list (пагинация last_id) → агрегат по sku: средний рейтинг, число отзывов,
распределение 1–5. Кладём в ozon_rating. Низкий рейтинг (<4.3) = карточка хуже продаётся.

Запуск:  ./venv/bin/python collectors/ozon_reviews.py [oz_acc1]
"""
import sys
import time
import pathlib
from collections import defaultdict

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                       # noqa: E402
from collectors.ozon import _headers      # seller-креды  # noqa: E402

URL = "https://api-seller.ozon.ru/v1/review/list"


def fetch(account):
    H = _headers(account)
    agg = defaultdict(lambda: {"sum": 0, "n": 0, "r": [0, 0, 0, 0, 0]})
    last_id, total = "", 0
    while True:
        body = {"limit": 100, "sort_dir": "DESC", "status": "ALL"}
        if last_id:
            body["last_id"] = last_id
        r = requests.post(URL, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        j = r.json()
        reviews = j.get("reviews", [])
        for rv in reviews:
            sku = str(rv.get("sku"))
            rt = rv.get("rating") or 0
            a = agg[sku]
            a["sum"] += rt
            a["n"] += 1
            if 1 <= rt <= 5:
                a["r"][rt - 1] += 1
        total += len(reviews)
        last_id = j.get("last_id")
        if not reviews or not last_id or len(reviews) < 100:
            break
        time.sleep(0.25)
    print(f"  отзывов обработано: {total}, товаров: {len(agg)}", flush=True)
    return agg


def main(account="oz_acc1"):
    print(f"Ozon отзывы/рейтинг {account}", flush=True)
    agg = fetch(account)
    recs = []
    for sku, a in agg.items():
        if a["n"] == 0:
            continue
        recs.append({"account": account, "sku": sku,
                     "avg_rating": round(a["sum"] / a["n"], 2), "reviews_count": a["n"],
                     "r1": a["r"][0], "r2": a["r"][1], "r3": a["r"][2],
                     "r4": a["r"][3], "r5": a["r"][4]})
    n = db.upsert("ozon_rating", recs, conflict_cols=["account", "sku"])
    low = sum(1 for r in recs if r["avg_rating"] < 4.3)
    print(f"Записано: {n} SKU | с рейтингом <4.3: {low}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")

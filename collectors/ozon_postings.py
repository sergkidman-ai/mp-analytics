"""collectors/ozon_postings.py — постинги Ozon (заказы) с financial_data → raw_ozon_posting.

Отдельный источник от транзакций: здесь есть ЦЕНЫ. На каждый товар постинга:
  price (по которой продали), old_price (до скидки), total_discount_value/percent,
  actions (в каких акциях; часть «за счёт Озон»), commission, payout.

FBS: POST /v3/posting/fbs/list (result.postings + has_next, пагинация offset).
FBO: POST /v2/posting/fbo/list (result — список, пагинация offset).

Запуск:  ./venv/bin/python collectors/ozon_postings.py 2026-06-01 2026-06-30 [oz_acc1]
"""
import sys
import time
import pathlib

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                       # noqa: E402
from collectors.ozon import _headers      # переиспользуем креды  # noqa: E402

FBS_URL = "https://api-seller.ozon.ru/v3/posting/fbs/list"
FBO_URL = "https://api-seller.ozon.ru/v2/posting/fbo/list"


def fetch_postings(account, scheme, date_from, date_to):
    """Все постинги схемы (fbs|fbo) за период, пагинация по offset."""
    H = _headers(account)
    url = FBS_URL if scheme == "fbs" else FBO_URL
    out, off = [], 0
    while True:
        flt = {"since": f"{date_from}T00:00:00.000Z", "to": f"{date_to}T23:59:59.999Z"}
        if scheme == "fbs":
            flt["status"] = ""
        body = {"dir": "ASC", "filter": flt, "limit": 1000, "offset": off,
                "with": {"financial_data": True}}
        r = requests.post(url, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        res = r.json().get("result")
        posts = (res.get("postings") if isinstance(res, dict) else res) or []
        out.extend(posts)
        off += len(posts)
        has_next = res.get("has_next") if isinstance(res, dict) else (len(posts) == 1000)
        print(f"  [oz {scheme}] +{len(posts)} (всего {len(out)})", flush=True)
        if not posts or not has_next:
            break
        time.sleep(0.3)
    return out


def load_raw(account, posts, scheme, date_from, date_to):
    recs = [{"account": account, "posting_number": p.get("posting_number"), "scheme": scheme,
             "status": p.get("status"),
             "in_process_at": p.get("in_process_at") or p.get("created_at"),
             "period_from": date_from, "period_to": date_to,
             "payload": psycopg2.extras.Json(p)}
            for p in posts if p.get("posting_number")]
    return db.upsert("raw_ozon_posting", recs, conflict_cols=["account", "posting_number"])


def main(date_from="2026-06-01", date_to="2026-06-30", account="oz_acc1"):
    print(f"Ozon постинги {account} {date_from}..{date_to}", flush=True)
    total = 0
    for scheme in ("fbs", "fbo"):
        posts = fetch_postings(account, scheme, date_from, date_to)
        total += load_raw(account, posts, scheme, date_from, date_to)
    print(f"Итого постингов → raw_ozon_posting: {total}", flush=True)


if __name__ == "__main__":
    a = sys.argv
    main(a[1] if len(a) > 1 else "2026-06-01",
         a[2] if len(a) > 2 else "2026-06-30",
         a[3] if len(a) > 3 else "oz_acc1")

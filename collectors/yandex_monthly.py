"""collectors/yandex_monthly.py — Яндекс.Маркет помесячно (выручка/субсидия/заказы).

Бизнес-эндпоинт /orders отдаёт ~30 дней. Для ИСТОРИИ используем /campaigns/{id}/stats/orders
(принимает dateFrom/dateTo, отдаёт месяцы назад). Выручка = Σ payment (наша цена, что заплатил
покупатель) без CANCELLED — сходится с учётной таблицей. Субсидия — доплата Маркета сверху (справочно).
Агрегат по месяцу creationDate, по кабинету (ya_acc1, все магазины campaignId). → yandex_monthly.

Запуск:  ./venv/bin/python collectors/yandex_monthly.py [YYYY-MM-01 since]
"""
import os
import sys
import time
import datetime
import pathlib
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"


def _cfg():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    camps = [c.strip() for c in (os.getenv("YANDEX_CAMPAIGN_ID_ACC1") or "").split(",") if c.strip()]
    if not key or not camps:
        raise RuntimeError("YANDEX_API_KEY_ACC1 / YANDEX_CAMPAIGN_ID_ACC1 не заданы")
    return key, camps


def collect(since="2026-01-01"):
    key, camps = _cfg()
    H = {"Api-Key": key, "Content-Type": "application/json"}
    today = datetime.date.today().isoformat()
    agg = defaultdict(lambda: {"revenue": 0.0, "subsidy": 0.0, "orders": 0})
    for cid in camps:
        tok = None
        for _ in range(200):
            params = {"page_token": tok} if tok else {}
            r = requests.post(f"{API}/campaigns/{cid}/stats/orders", headers=H, params=params,
                              json={"dateFrom": since, "dateTo": today}, timeout=120)
            if r.status_code != 200:
                print(f"  [ya monthly] cid {cid}: HTTP {r.status_code} — стоп", flush=True)
                break
            res = r.json().get("result", {})
            for o in res.get("orders", []):
                if o.get("status") == "CANCELLED":
                    continue
                cd = (o.get("creationDate") or "")[:7]   # YYYY-MM
                if len(cd) != 7:
                    continue
                mo = cd + "-01"
                a = agg[mo]
                a["orders"] += 1
                for p in (o.get("payments") or []):
                    a["revenue"] += p.get("total", 0) or 0
                for s in (o.get("subsidies") or []):
                    a["subsidy"] += s.get("amount", 0) or 0
            tok = (res.get("paging") or {}).get("nextPageToken")
            if not tok:
                break
            time.sleep(0.5)
    recs = [{"account": ACCOUNT, "month": mo, "revenue": round(v["revenue"], 2),
             "subsidy": round(v["subsidy"], 2), "orders": v["orders"]}
            for mo, v in sorted(agg.items())]
    if recs:
        db.upsert("yandex_monthly", recs, conflict_cols=["account", "month"],
                  update_cols=["revenue", "subsidy", "orders"])
    for r in recs:
        print(f"  {r['month'][:7]}: выручка {r['revenue']:,.0f} | субсидия {r['subsidy']:,.0f} | заказов {r['orders']}", flush=True)
    print(f"Яндекс.Маркет помесячно: {len(recs)} месяцев записано", flush=True)


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    print(f"Яндекс.Маркет помесячно с {since} (stats/orders)", flush=True)
    collect(since)


if __name__ == "__main__":
    main()

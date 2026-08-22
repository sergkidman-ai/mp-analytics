# поток: ev
"""yandex_stocks.py — снимок остатков на складах Яндекс.Маркета по ВСЕМ активным магазинам.

Правка 22.08.2026 (Наталья): собираем не только FBY. У нас 7 FBS-магазинов, у каждого свой склад,
и остатки живут именно там; FBY-магазин пустой и с выключенным в ЛК доступом к API.
Список магазинов берём из `GET /campaigns` — новый магазин подхватится сам.

Остаток по FBS — это НАШ склад, который мы передаём Маркету, а не хранение у Маркета.
Что реально лежит у Маркета (невыкупы и возвраты) — в потоке `ret` (mp_returns), см.
reports/ya_removal_candidates.py.

Пишет в ya_mp_stock (миграции 507 + 508), один снимок на день.
"""
import datetime
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db  # noqa: E402

API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"
PAGE = 200
STOCK_TYPES = {"AVAILABLE": "available", "FREEZE": "frozen", "DEFECT": "defect", "EXPIRED": "expired"}


def _headers():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    if not key:
        raise RuntimeError("нет YANDEX_API_KEY_ACC1 в .env")
    return {"Api-Key": key, "Content-Type": "application/json"}


def campaigns():
    """[(id, placementType, name)] — все магазины кабинета."""
    r = requests.get(f"{API}/campaigns", headers=_headers(),
                     params={"page": 1, "pageSize": 50}, timeout=60)
    r.raise_for_status()
    return [(c["id"], c.get("placementType"), c.get("domain") or str(c["id"]))
            for c in r.json().get("campaigns", [])]


def fetch(campaign_id):
    """Остатки одного магазина. None — если у магазина выключен доступ к API."""
    head, out, token = _headers(), [], None
    while True:
        params = {"limit": PAGE}
        if token:
            params["page_token"] = token
        r = requests.post(f"{API}/campaigns/{campaign_id}/offers/stocks", headers=head,
                          params=params, json={"withTurnover": True}, timeout=60)
        if r.status_code == 403 and "API_DISABLED" in r.text:
            return None
        r.raise_for_status()
        res = r.json().get("result") or {}
        out += res.get("warehouses") or []
        token = (res.get("paging") or {}).get("nextPageToken")
        if not token:
            return out
        time.sleep(0.3)


def main():
    day = datetime.date.today()
    rows, skipped, seen = [], [], 0
    for cid, placement, name in campaigns():
        whs = fetch(cid)
        if whs is None:
            skipped.append(f"{name} ({placement})")
            continue
        for w in whs:
            for o in w.get("offers", []):
                seen += 1
                cnt = {v: 0 for v in STOCK_TYPES.values()}
                for s in o.get("stocks", []):
                    k = STOCK_TYPES.get(s.get("type"))
                    if k:
                        cnt[k] += s.get("count") or 0
                if not any(cnt.values()):
                    continue          # нули не храним: 135 тыс. строк на снимок против 20 тыс.
                turn = o.get("turnoverSummary") or {}
                rows.append({
                    "account": ACCOUNT, "campaign_id": cid, "campaign_name": name,
                    "placement": placement, "warehouse_id": w.get("warehouseId"),
                    "warehouse": w.get("name"), "offer_id": o.get("offerId"),
                    "turnover_days": turn.get("turnoverDays"), "turnover": turn.get("turnover"),
                    "updated_at": o.get("updatedAt"), "captured_at": day, **cnt})
    if rows:
        db.upsert("ya_mp_stock", rows,
                  ["account", "campaign_id", "warehouse_id", "offer_id", "captured_at"])
    qty = sum(r["available"] + r["frozen"] for r in rows)
    print(f"{day}: магазинов с данными {len({r['campaign_id'] for r in rows})}, "
          f"позиций с остатком {len(rows)} (из {seen} в каталоге), штук {qty}")
    if skipped:
        print("пропущены (в ЛК Маркета выключен доступ к API): " + ", ".join(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())

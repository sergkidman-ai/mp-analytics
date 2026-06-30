"""collectors/ozon_products.py — каталог товаров Ozon: sku → имя + флаг архива.

/v3/product/list (visibility=ALL) → product_id, затем /v3/product/info/list → name,
is_archived/is_autoarchived, sku + sources[].sku. Кладём по каждому sku в ozon_product.
Нужен для рейтинга карточек: архивные не показываем, живым безымянным даём настоящее имя.

Запуск:  ./venv/bin/python collectors/ozon_products.py [oz_acc1]
"""
import sys
import time
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                              # noqa: E402
from collectors.ozon import _headers, PRODUCT_LIST_URL, PRODUCT_INFO_URL  # noqa: E402


def _list_pids(H, visibility):
    pids, last = [], ""
    while True:
        r = requests.post(PRODUCT_LIST_URL, headers=H,
                          json={"filter": {"visibility": visibility}, "last_id": last, "limit": 1000}, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        res = r.json()["result"]
        items = res.get("items") or []
        pids += [i["product_id"] for i in items]
        last = res.get("last_id") or ""
        if len(items) < 1000:
            break
    return pids


def _info(H, pids, archived, by_sku):
    for i in range(0, len(pids), 1000):
        r = requests.post(PRODUCT_INFO_URL, headers=H, json={"product_id": pids[i:i + 1000]}, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            r = requests.post(PRODUCT_INFO_URL, headers=H, json={"product_id": pids[i:i + 1000]}, timeout=120)
        r.raise_for_status()
        for it in r.json().get("items", []):
            name = it.get("name")
            offer = it.get("offer_id")
            for sku in [it.get("sku")] + [s.get("sku") for s in (it.get("sources") or [])]:
                if sku:
                    by_sku[str(sku)] = {"sku": str(sku), "offer_id": offer, "name": name,
                                        "is_archived": archived}
        time.sleep(0.3)


def fetch(account):
    """Активные (visibility=ALL → is_archived=false) + архивные (visibility=ARCHIVED → true).
    В Ozon ALL не включает архив — для него отдельный фильтр."""
    H = _headers(account)
    by_sku = {}
    _info(H, _list_pids(H, "ALL"), False, by_sku)        # активные сперва
    _info(H, _list_pids(H, "ARCHIVED"), True, by_sku)    # затем архивные (помечаем)
    return [{"account": account, **v} for v in by_sku.values()]


def main(account="oz_acc1"):
    print(f"Ozon каталог {account}", flush=True)
    recs = fetch(account)
    n = db.upsert("ozon_product", recs, conflict_cols=["account", "sku"],
                  update_cols=["offer_id", "name", "is_archived"])
    arch = sum(1 for r in recs if r["is_archived"])
    print(f"Записано sku: {n} | в архиве: {arch}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")

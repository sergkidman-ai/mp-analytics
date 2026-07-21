"""collectors/wb_market_price.py — реальная цена покупателя (после СПП) из публичного card.wb.ru v4.

Prices API (wb_price.discounted_price) даёт акционную цену, но ДО СПП → как цену покупателя завышает.
Публичный v4 отдаёт sizes[].price.product = цена ПОСЛЕ акции И СПП (что реально видит/платит покупатель).
Проверено: 216421567 basic 2671 → product 1859 (промо 13% + СПП 20%); 200167236 → 2039.
Параметр spp= в запросе на product НЕ влияет (проверено 0/30). СПП%карточки = 1 − product/discountedPrice.

Читает список nm из wb_price, батчит по 100 через ;, кладёт market_price/market_basic в wb_price.
Токен НЕ нужен (публичный API). Запуск: ./venv/bin/python collectors/wb_market_price.py [wb_acc1]
"""
import os
import sys
import time
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
V4 = "https://card.wb.ru/cards/v4/detail"
BATCH = 100


def _fetch(nms):
    r = requests.get(V4, params={"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30,
                                 "nm": ";".join(str(n) for n in nms)}, timeout=30)
    if r.status_code != 200:
        return {}
    out = {}
    for p in (r.json().get("products") or []):
        sz = (p.get("sizes") or [{}])[0]
        price = sz.get("price") or {}
        prod = price.get("product")
        basic = price.get("basic")
        if prod:
            out[int(p["id"])] = (prod / 100.0, (basic / 100.0 if basic else None))
    return out


def main(account="wb_acc1"):
    nms = [int(r["nm_id"]) for r in db.query(
        "SELECT nm_id FROM wb_price WHERE account=%s ORDER BY nm_id", (account,))]
    total, got = 0, 0
    for i in range(0, len(nms), BATCH):
        chunk = nms[i:i + BATCH]
        try:
            prices = _fetch(chunk)
        except Exception as e:
            print(f"  [market {account}] batch {i} err {e}", flush=True)
            time.sleep(2)
            continue
        recs = [{"account": account, "nm_id": nm, "market_price": pr, "market_basic": bs,
                 "market_captured_at": "now()"} for nm, (pr, bs) in prices.items()]
        if recs:
            # обновляем только рыночные поля, не трогая цены Prices API
            for rc in recs:
                db.execute("""UPDATE wb_price SET market_price=%(market_price)s, market_basic=%(market_basic)s,
                                     market_captured_at=now()
                              WHERE account=%(account)s AND nm_id=%(nm_id)s""", rc)
            got += len(recs)
        total += len(chunk)
        if i % (BATCH * 10) == 0:
            print(f"  [market {account}] {total}/{len(nms)} обработано, цен получено {got}", flush=True)
        time.sleep(0.25)
    print(f"[wb_market_price {account}] nm {len(nms)}, рыночных цен получено {got}", flush=True)
    return got


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    main(acc)

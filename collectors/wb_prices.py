"""collectors/wb_prices.py — текущие карточные цены WB (Prices API) → таблица wb_price.

discounts-prices-api.wildberries.ru, скоуп «Цены и скидки» = ОТДЕЛЬНЫЙ токен WB_TOKEN_PRICES_*.
GET /api/v2/list/goods/filter?limit=1000&offset= — пагинация по всему каталогу (фильтр 1 nm: filterNmID=).
Поля: nmID, vendorCode, sizes[].price (ДО акции, list), sizes[].discountedPrice (АКЦИОННАЯ цена = после
промо, ДО СПП — это БАЗА комиссии/СПП; НЕ цена покупателя, СПП тут не учтён), discount(% акции).
Цены в РУБЛЯХ. Идемпотентно: upsert по (account, nm_id).

Запуск:  ./venv/bin/python collectors/wb_prices.py [wb_acc1]
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
B = "https://discounts-prices-api.wildberries.ru"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_PRICES_ACC1", "wb_acc2": "WB_TOKEN_PRICES_ACC2"}
PAGE = 1000


def _token(account):
    t = os.getenv(TOKEN_ENV[account])
    if not t:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан в .env (скоуп «Цены и скидки»)")
    return t


def main(account="wb_acc1"):
    H = {"Authorization": _token(account)}
    offset, total = 0, 0
    while True:
        r = requests.get(B + "/api/v2/list/goods/filter", headers=H,
                         params={"limit": PAGE, "offset": offset}, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("X-Ratelimit-Retry", "10")) + 1)
            continue
        if r.status_code != 200:
            print(f"WB цены {account}: HTTP {r.status_code} offset {offset} — {r.text[:180]}", flush=True)
            break
        goods = (r.json().get("data") or {}).get("listGoods") or []
        if not goods:
            break
        recs = []
        for g in goods:
            sizes = g.get("sizes") or [{}]
            s = sizes[0]  # у картриджей один размер
            recs.append({
                "account": account,
                "nm_id": g.get("nmID"),
                "vendor_code": g.get("vendorCode"),
                "price": s.get("price"),
                "discounted_price": s.get("discountedPrice"),
                "discount_pct": g.get("discount"),
                "club_price": s.get("clubDiscountedPrice"),
                "currency": g.get("currencyIsoCode4217") or "RUB",
            })
        db.upsert("wb_price", recs, conflict_cols=["account", "nm_id"])
        total += len(recs)
        offset += PAGE
        print(f"  [wb_prices {account}] offset {offset}: +{len(recs)} (всего {total})", flush=True)
        if len(goods) < PAGE:
            break
        time.sleep(0.3)
    print(f"[wb_prices {account}] записано цен: {total}", flush=True)
    return total


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    main(acc)

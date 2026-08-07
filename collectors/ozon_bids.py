# поток: mkt
"""collectors/ozon_bids.py — состав рекламных кампаний Ozon и ставки по SKU (снимок на дату).

Два разных источника, потому что у Ozon два разных механизма продвижения:

1. Кампании с оплатой за клик (`advObjectType = SKU`):
   `GET /api/client/campaign/{id}/v2/products?page=&pageSize=` → products[{sku, bid, title,
   targetCir}] — цены товара здесь нет. **Пагинация обязательна:** без параметров эндпоинт отдаёт
   ровно 30 строк (дефолт страницы), а в кампании бывает 8 000 товаров. Страницы —
   с 1-й, по 1000, пока страница полная.

2. Продвижение в поиске и «Оплата за заказ» (`SEARCH_PROMO`, `ALL_SKU_PROMO`):
   на `/v2/products` отвечают 400 — у них
   `POST /api/client/campaign/search_promo/v2/products {campaignId, page, pageSize}`.
   Эндпоинт отдаёт НЕ состав кампании, а весь пул товаров аккаунта, доступных продвижению
   в поиске (выдача одинакова для любого campaignId, в т.ч. несуществующего) → пишется
   один раз на аккаунт в отдельную таблицу `ozon_search_promo`, вместе с индексом видимости.

Берутся ВСЕ кампании, кроме архивных и завершённых (архив состав всё ещё отдаёт, но это
история): состояние кампании пишется в `ozon_bids.state`, фильтрация — на стороне витрин.

Запуск:  ./venv/bin/python collectors/ozon_bids.py [oz_acc1|oz_acc2|all]
"""
import datetime
import pathlib
import sys
import time

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                          # noqa: E402
from collectors.ozon_ads import has_creds, _token, PERF      # noqa: E402

PAGE = 1000                                                  # потолок страницы у обоих эндпоинтов
SKIP_STATES = {"CAMPAIGN_STATE_ARCHIVED", "CAMPAIGN_STATE_FINISHED"}
SEARCH_TYPES = {"SEARCH_PROMO", "ALL_SKU_PROMO"}
MICRO = 1_000_000                                            # ставки приходят в микрорублях


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _campaign_products(H, cid):
    """Состав SKU-кампании целиком, страницами по PAGE. Страницы нумеруются с 1."""
    out, page = [], 1
    while True:
        r = requests.get(f"{PERF}/api/client/campaign/{cid}/v2/products", headers=H,
                         timeout=120, params={"page": page, "pageSize": PAGE})
        if r.status_code != 200:
            return out, r.status_code
        chunk = r.json().get("products") or []
        out += chunk
        if len(chunk) < PAGE or page > 50:
            return out, 200
        page += 1
        time.sleep(0.2)


def _search_promo(H):
    """Пул продвижения в поиске — весь аккаунт. Страницы с 0, в выдаче бывают дубли."""
    seen, page = {}, 0
    while page <= 50:
        r = requests.post(f"{PERF}/api/client/campaign/search_promo/v2/products", headers=H,
                          timeout=120, json={"page": page, "pageSize": PAGE})
        if r.status_code != 200:
            break
        chunk = r.json().get("products") or []
        for p in chunk:
            if p.get("sku"):
                seen[str(p["sku"])] = p
        if len(chunk) < PAGE:
            break
        page += 1
        time.sleep(0.2)
    return list(seen.values())


def fetch(account):
    H = {"Authorization": f"Bearer {_token(account)}"}
    camps = requests.get(f"{PERF}/api/client/campaign", headers=H, timeout=60).json().get("list", [])
    cap = datetime.date.today().isoformat()
    recs, skipped, sp_camps = [], [], 0

    for c in camps:
        if c.get("state") in SKIP_STATES:
            continue
        if c.get("advObjectType") in SEARCH_TYPES:
            sp_camps += 1                       # состав берётся общим списком, не по кампании
            continue
        prods, code = _campaign_products(H, c["id"])
        if code != 200 and not prods:
            skipped.append((str(c["id"]), c.get("advObjectType"), code))
            continue
        for p in prods:
            if not p.get("sku"):
                continue
            recs.append({"account": account, "campaign_id": str(c["id"]),
                         "campaign_title": c.get("title"), "adv_type": c.get("advObjectType"),
                         "state": c.get("state"), "sku": str(p["sku"]), "title": p.get("title"),
                         "bid": round(int(p.get("bid") or 0) / MICRO, 2),
                         "target_cir": p.get("targetCir") or 0, "captured_at": cap})
        time.sleep(0.2)

    sp = []
    if sp_camps:
        for p in _search_promo(H):
            vw = p.get("views") if isinstance(p.get("views"), dict) else {}
            sp.append({"account": account, "sku": str(p["sku"]), "captured_at": cap,
                       "source_sku": p.get("sourceSku"), "title": p.get("title"),
                       "price": _num(p.get("price")),
                       "bid": round(int(p.get("bid") or 0) / MICRO, 2),
                       "bid_without_additive": round(int(p.get("bidWithoutAdditive") or 0) / MICRO, 2),
                       "carrots_additive": round(int(p.get("carrotsAdditive") or 0) / MICRO, 2),
                       "views_week": int(_num(vw.get("thisWeek")) or 0),
                       "views_prev_week": int(_num(vw.get("previousWeek")) or 0),
                       "visibility_index": p.get("visibilityIndex"),
                       "prev_visibility_index": p.get("previousVisibilityIndex"),
                       "promo_status": bool(p.get("searchPromoStatus")),
                       "available": bool(p.get("isSearchPromoAvailable")),
                       "carrots_status": p.get("carrotsStatus")})
    return recs, sp, camps, skipped, sp_camps


def main(account="oz_acc1"):
    if account == "all":
        for a in ("oz_acc1", "oz_acc2"):
            main(a)
        return
    if not has_creds(account):
        print(f"Ozon ставки {account}: нет Performance-кредов — пропуск", flush=True)
        return
    print(f"Ozon ставки {account}", flush=True)
    recs, sp, camps, skipped, sp_camps = fetch(account)
    n = db.upsert("ozon_bids", recs, conflict_cols=["account", "campaign_id", "sku", "captured_at"],
                  update_cols=["campaign_title", "adv_type", "state", "title", "bid",
                               "target_cir"]) if recs else 0
    m = db.upsert("ozon_search_promo", sp, conflict_cols=["account", "sku", "captured_at"],
                  update_cols=["source_sku", "title", "price", "bid", "bid_without_additive",
                               "carrots_additive", "views_week", "views_prev_week",
                               "visibility_index",
                               "prev_visibility_index", "promo_status", "available",
                               "carrots_status"]) if sp else 0
    print(f"  кампаний в кабинете {len(camps)}, взято по составу "
          f"{len({r['campaign_id'] for r in recs})}, поисковых {sp_camps}", flush=True)
    print(f"  ставок записано {n} (уникальных SKU {len({r['sku'] for r in recs})}), "
          f"пул продвижения в поиске {m}", flush=True)
    if skipped:
        print(f"  не отдали состав: {skipped[:6]}{' …' if len(skipped) > 6 else ''}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")

# поток: mkt
"""collectors/ozon_price_index.py — ценовые индексы Ozon по каждому товару (снимок на дату).

POST /v5/product/info/prices — за один вызов отдаёт по каждой карточке сразу:
  * `price_indexes.color_index` — зона индекса цен (наблюдались SUPER / GREEN / YELLOW /
    RED / WITHOUT_INDEX);
  * три индекса, у каждого своя `min_price` — **фактическая минимальная цена рынка**,
    то есть цена конкурента в рублях по этому товару:
      external_index_data          — против цен на других площадках (работает у нас),
      ozon_index_data              — против других продавцов того же товара на Ozon
                                     (по нашему ассортименту почти везде нули),
      self_marketplaces_index_data — против наших же цен на других маркетплейсах;
  * цены (price / old_price / marketing_price), `commissions`, `acquiring`, `volume_weight`.

Зачем: буст в поиске работает ТОЛЬКО у товаров с выгодным индексом цен. По разведке
28.07.2026 около половины ассортимента сидит в красной зоне — на этой половине подписка
не даёт ничего (docs/ozon-recon-report.md, разделы 4.1 и 9.3). Витрины красной зоны
у нас нет, и сам индекс до сих пор нигде не собирался.

Снимки, а не текущее состояние: индекс двигается вслед за конкурентом, и без истории
не отличить «мы подняли цену» от «рынок опустил свою». Ключ (account, sku, collected_on),
повторный прогон в тот же день перезаписывает снимок этого дня.

Осторожно с именами полей: `min_price` в блоке цен — это НАША установленная минимальная
цена, а `min_price` внутри *_index_data — цена рынка. Разные вещи, в таблице разведены.

Запуск:
    ./venv/bin/python collectors/ozon_price_index.py            # оба аккаунта
    ./venv/bin/python collectors/ozon_price_index.py oz_acc1    # один
    ./venv/bin/python collectors/ozon_price_index.py oz_acc1 --probe   # 10 карточек, без записи
"""
import datetime
import json
import os
import pathlib
import sys
import time

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = "https://api-seller.ozon.ru"
PRICES_PATH = "/v5/product/info/prices"
CRED_ENV = {"oz_acc1": ("OZON_CLIENT_ID_ACC1", "OZON_API_KEY_ACC1"),
            "oz_acc2": ("OZON_CLIENT_ID_ACC2", "OZON_API_KEY_ACC2")}
ACCOUNTS = ["oz_acc1", "oz_acc2"]

PAGE_LIMIT = 1000       # потолок метода
PAUSE = 0.55            # 2 запроса/сек — общий лимитер Ozon
MAX_PAGES = 500         # предохранитель от бесконечного курсора


def _headers(account):
    cid_env, key_env = CRED_ENV[account]
    cid, key = os.getenv(cid_env), os.getenv(key_env)
    if not cid or not key:
        raise RuntimeError(f"{cid_env}/{key_env} не заданы в .env")
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}


class Api:
    """Клиент с лимитером и счётчиком вызовов (счётчик уходит в журнал прогонов)."""

    def __init__(self, account):
        self.account = account
        self.headers = _headers(account)
        self.calls = 0

    def post(self, path, body, tries=4):
        r = None
        for attempt in range(tries):
            r = requests.post(API + path, headers=self.headers, json=body, timeout=120)
            self.calls += 1
            time.sleep(PAUSE)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code >= 500:
                time.sleep(3 + attempt * 3)
                continue
            return r.status_code, (r.json() if r.content else {})
        return r.status_code, (r.json() if r.content else {})


def _num(x):
    """Ozon отдаёт цены СТРОКАМИ ("1563.0000"), пустое значение — "" или отсутствует."""
    if x in (None, "", "0", 0):
        return None if x in (None, "") else 0
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pick(d, *names):
    """Поле у Ozon переезжало между версиями (min_price / minimal_price) — берём что есть."""
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    return None


def _index_block(idx, key):
    b = idx.get(key) or {}
    return (_num(_pick(b, "min_price", "minimal_price")),
            _num(_pick(b, "price_index_value", "index_value")))


def _row(account, item, today):
    price = item.get("price") or {}
    idx = item.get("price_indexes") or {}
    comm = item.get("commissions") or {}
    ext_min, ext_val = _index_block(idx, "external_index_data")
    oz_min, oz_val = _index_block(idx, "ozon_index_data")
    self_min, self_val = _index_block(idx, "self_marketplaces_index_data")
    return {
        "account": account,
        "sku": str(item.get("product_id") or ""),
        "offer_id": item.get("offer_id") or "",
        "collected_on": today,
        "price": _num(price.get("price")),
        "old_price": _num(price.get("old_price")),
        "marketing_price": _num(price.get("marketing_price")),
        "marketing_seller_price": _num(price.get("marketing_seller_price")),
        "min_price": _num(price.get("min_price")),
        "currency": price.get("currency_code") or None,
        "auto_action_enabled": bool(price.get("auto_action_enabled")),
        "color_index": idx.get("color_index") or None,
        "external_min_price": ext_min, "external_index": ext_val,
        "ozon_min_price": oz_min, "ozon_index": oz_val,
        "self_min_price": self_min, "self_index": self_val,
        "commission_fbo_pct": _num(comm.get("sales_percent_fbo")),
        "commission_fbs_pct": _num(comm.get("sales_percent_fbs")),
        "acquiring": _num(item.get("acquiring")),
        "volume_weight": _num(item.get("volume_weight")),
    }


COLS = ["account", "sku", "offer_id", "collected_on", "price", "old_price", "marketing_price",
        "marketing_seller_price", "min_price", "currency", "auto_action_enabled", "color_index",
        "external_min_price", "external_index", "ozon_min_price", "ozon_index",
        "self_min_price", "self_index", "commission_fbo_pct", "commission_fbs_pct",
        "acquiring", "volume_weight"]


def fetch(api, probe=False):
    """Постранично забирает весь ассортимент аккаунта. Пагинация — курсором."""
    cursor, pages, items = "", 0, []
    limit = 10 if probe else PAGE_LIMIT
    while pages < MAX_PAGES:
        body = {"cursor": cursor, "limit": limit, "filter": {"visibility": "ALL"}}
        code, data = api.post(PRICES_PATH, body)
        if code != 200:
            raise RuntimeError(f"{PRICES_PATH} → {code}: {json.dumps(data, ensure_ascii=False)[:300]}")
        batch = data.get("items") or []
        items.extend(batch)
        pages += 1
        cursor = data.get("cursor") or ""
        print(f"    страница {pages}: +{len(batch)} → всего {len(items)}", flush=True)
        if probe or not batch or not cursor:
            break
    return items


def main(account="oz_acc1", probe=False):
    api = Api(account)
    today = datetime.date.today()
    items = fetch(api, probe=probe)
    if not items:
        print(f"[{account}] метод вернул пусто — нечего писать", flush=True)
        return

    rows = [_row(account, it, today) for it in items]
    rows = [r for r in rows if r["sku"]]
    zones, with_ext = {}, 0
    for r in rows:
        zones[r["color_index"] or "NULL"] = zones.get(r["color_index"] or "NULL", 0) + 1
        if r["external_min_price"]:
            with_ext += 1

    if probe:
        print(f"[{account}] ПРОБА, без записи: карточек {len(rows)}, зоны {zones}", flush=True)
        print(f"  пример: {json.dumps(rows[0], ensure_ascii=False, default=str)[:400]}", flush=True)
        return

    db.upsert("ozon_price_index", rows, ["account", "sku", "collected_on"],
              [c for c in COLS if c not in ("account", "sku", "collected_on")])
    db.upsert("ozon_price_index_run",
              [{"account": account, "collected_on": today, "items": len(rows),
                "with_external": with_ext, "zones": json.dumps(zones, ensure_ascii=False),
                "api_calls": api.calls}],
              ["account", "collected_on"], ["items", "with_external", "zones", "api_calls"])
    red = zones.get("RED", 0)
    print(f"[{account}] записано {len(rows)} карточек, с внешним индексом {with_ext}, "
          f"вызовов {api.calls}", flush=True)
    print(f"  зоны: {zones}"
          f"{f' — красная {100*red/len(rows):.1f}%' if rows else ''}", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    probe = "--probe" in args
    positional = [a for a in args if not a.startswith("--")]
    target = positional[0] if positional else "all"
    for acc in (ACCOUNTS if target == "all" else [target]):
        main(acc, probe=probe)

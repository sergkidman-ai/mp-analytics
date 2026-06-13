"""collectors/moysklad.py — Этап 1. Сбор товаров МойСклад → raw → products.

МойСклад = источник правды по товарам и себестоимости (раздел 2 ARCHITECTURE.md).
- Грузим ВСЕ товары (вкл. архивные) в raw_moysklad_product — полное сырьё (JSONB,
  со всеми кастомными атрибутами: «Название WB», «Связь», габариты, бренд и т.д.).
- Нормализуем в products ТОЛЬКО активные (archived=false). Идентификаторы МойСклада
  сохраняем под их именами: article (=МС article), code (=МС code),
  external_code (=МС externalCode). PK = ms_id (code НЕ уникален).
- Себестоимость группы (MIN по карточкам external_code) считается в витрине маржи, не здесь.
- Идемпотентность: UPSERT по ms_id — повторный запуск обновляет цену/название и
  добавляет новинки, без дублей.

Запуск:  ./venv/bin/python collectors/moysklad.py
"""
import os
import sys
import time
import pathlib

import requests
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("MOYSKLAD_TOKEN")
if not TOKEN:
    raise RuntimeError("MOYSKLAD_TOKEN не задан в .env")

API = "https://api.moysklad.ru/api/remap/1.2"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept-Encoding": "gzip",                    # обязателен для МойСклада (иначе 415)
    "Accept": "application/json;charset=utf-8",    # именно это значение (иначе 400/1062)
}
PAGE_LIMIT = 1000          # макс. limit МойСклада
MIN_INTERVAL = 0.3         # пауза между запросами (~3 req/s, ниже лимита 11/3 сек)

# Имена кастомных атрибутов карточки (из entity/product/metadata/attributes)
ATTR_LEN, ATTR_WID, ATTR_HGT = "Длина, см.", "Ширина, см.", "Высота, см."

_last_req = [0.0]


def _throttle():
    dt = time.monotonic() - _last_req[0]
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    _last_req[0] = time.monotonic()


def _get(url, params=None, _tries=0):
    _throttle()
    r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    if r.status_code == 429 and _tries < 6:
        # лимит запросов: ждём по заголовку (мс), затем повтор
        wait_ms = int(r.headers.get("X-Lognex-Retry-TimeInterval", "1000"))
        time.sleep(wait_ms / 1000.0 + 0.1)
        return _get(url, params, _tries + 1)
    r.raise_for_status()
    return r.json()


def fetch_all_products():
    """Все товары МойСклада постранично (limit/offset)."""
    out, offset = [], 0
    while True:
        data = _get(f"{API}/entity/product", params={"limit": PAGE_LIMIT, "offset": offset})
        rows = data.get("rows", [])
        out.extend(rows)
        size = data.get("meta", {}).get("size", 0)
        print(f"  [fetch] offset={offset} +{len(rows)} (всего {len(out)} из {size})", flush=True)
        offset += PAGE_LIMIT
        if not rows or offset >= size:
            break
    return out


def _attrs(p):
    """Кастомные атрибуты карточки как dict {имя: значение}."""
    out = {}
    for a in p.get("attributes", []):
        out[a.get("name")] = a.get("value")
    return out


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return None


def load_raw(products):
    """Все товары (вкл. архивные) → raw_moysklad_product (UPSERT по ms_id)."""
    rows = [{
        "ms_id": p.get("id"),
        "article": p.get("article"),
        "payload": psycopg2.extras.Json(p),
    } for p in products if p.get("id")]
    return db.upsert("raw_moysklad_product", rows, conflict_cols=["ms_id"])


def normalize_products(products):
    """Активные карточки → products (UPSERT по ms_id). Идентификаторы МС — под их именами."""
    rows = []
    for p in products:
        if p.get("archived"):
            continue
        a = _attrs(p)
        L, W, H = _num(a.get(ATTR_LEN)), _num(a.get(ATTR_WID)), _num(a.get(ATTR_HGT))
        volume_l = (L * W * H / 1000.0) if (L and W and H) else None   # см³ → л
        bp = (p.get("buyPrice") or {}).get("value")
        rows.append({
            "ms_id": p.get("id"),
            "article": p.get("article"),            # МС article (артикул производителя)
            "code": p.get("code"),                  # МС code (0002sk)
            "external_code": p.get("externalCode"),  # МС externalCode (0002 — группа)
            "title": p.get("name"),
            "category": p.get("pathName"),
            "buy_price": (bp / 100.0) if bp is not None else None,   # копейки → рубли
            "length_cm": L, "width_cm": W, "height_cm": H,
            "weight_kg": _num(p.get("weight")),
            "volume_l": volume_l,
        })
    db.upsert("products", rows, conflict_cols=["ms_id"])
    return len(rows)


def main():
    print("Сбор товаров МойСклад…", flush=True)
    products = fetch_all_products()
    n_raw = load_raw(products)
    n_active = normalize_products(products)
    print(f"\nИтого: получено {len(products)} карточек → raw {n_raw}, "
          f"активных в products {n_active}", flush=True)


if __name__ == "__main__":
    main()

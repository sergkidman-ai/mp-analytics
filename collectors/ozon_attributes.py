"""collectors/ozon_attributes.py — характеристики/описание карточек Ozon → raw_ozon_attributes.

Зачем: ozon_product несёт только имя+флаг архива. Для ответов на отзывы/вопросы нужен текст
родной карточки — атрибуты (совместимые модели, тип, ресурс, чип) и аннотация, которые видит
покупатель. Источник: POST /v4/product/info/attributes (пагинация last_id). Кладём объект
целиком в raw_ozon_attributes по offer_id (= МС code, тот же ключ, что использует grounding).

Идемпотентно: UPSERT по (account, offer_id). Только Премиум (oz_acc1) — где есть отзывы.

Запуск:  ./venv/bin/python collectors/ozon_attributes.py [oz_acc1]
"""
import sys
import time
import pathlib

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402
from collectors.ozon import _headers          # noqa: E402

ATTR_URL = "https://api-seller.ozon.ru/v4/product/info/attributes"


def fetch(account):
    """Все карточки постранично (last_id). Возвращает список объектов с attributes[]."""
    H = _headers(account)
    LIMIT = 1000
    out, last, seen = [], "", set()
    while True:
        body = {"filter": {"visibility": "ALL"}, "limit": LIMIT, "last_id": last, "sort_dir": "ASC"}
        r = requests.post(ATTR_URL, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        if r.status_code == 404:            # Ozon отдаёт 404 при запросе за последней страницей
            break
        r.raise_for_status()
        d = r.json()
        items = d.get("result") or []
        out.extend(items)
        last = d.get("last_id") or ""
        print(f"  [oz attr] +{len(items)} (всего {len(out)})", flush=True)
        if len(items) < LIMIT or not last or last in seen:   # конец / нет курсора / зацикливание
            break
        seen.add(last)
        time.sleep(0.3)
    return out


def load_raw(account, items):
    recs = []
    for it in items:
        offer = it.get("offer_id")
        if not offer:
            continue
        sku = it.get("sku")
        recs.append({"account": account, "offer_id": str(offer),
                     "sku": str(sku) if sku else None,
                     "payload": psycopg2.extras.Json(it)})
    return db.upsert("raw_ozon_attributes", recs, conflict_cols=["account", "offer_id"],
                     update_cols=["sku", "payload", "collected_at"])


def main(account="oz_acc1"):
    print(f"Ozon атрибуты карточек {account}", flush=True)
    items = fetch(account)
    n = load_raw(account, items)
    with_attr = sum(1 for it in items if it.get("attributes"))
    print(f"Записано карточек: {n} | с атрибутами: {with_attr}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")

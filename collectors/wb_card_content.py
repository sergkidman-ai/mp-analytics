"""collectors/wb_card_content.py — полный контент карточек WB (описание + характеристики) → raw.

Зачем: wb_cards несёт только заголовок+габариты. Для ответов на отзывы/вопросы нужен текст,
который видит покупатель — description и characteristics[] (чип, ресурс, совместимые модели).
Тот же эндпоинт content-api, что и wb.collect_cards, но кладём КАРТОЧКУ ЦЕЛИКОМ в
raw_wb_card_content. Разбор в фактические признаки — в reports/feedback_grounding.

Идемпотентно: UPSERT по (account, nm_id). Только Цифровой (wb_acc1) — у Дисквэра отзывов нет.

Запуск:  ./venv/bin/python collectors/wb_card_content.py [wb_acc1]
"""
import sys
import time
import pathlib

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                              # noqa: E402
from collectors.wb import CARDS_URL, _token      # noqa: E402


def fetch(account):
    """Все карточки постранично (курсор updatedAt+nmID). Возвращает список полных объектов."""
    H = {"Authorization": _token(account), "Content-Type": "application/json"}
    cursor, cards = {"limit": 100}, []
    while True:
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        r = requests.post(CARDS_URL, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 1)
            continue
        r.raise_for_status()
        d = r.json()
        batch = d.get("cards", [])
        cur = d.get("cursor", {})
        cards.extend(batch)
        print(f"  [wb cards] +{len(batch)} (всего {len(cards)})", flush=True)
        if len(batch) < cursor["limit"]:
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        time.sleep(0.3)
    return cards


def load_raw(account, cards):
    recs = [{"account": account, "nm_id": c.get("nmID"),
             "vendor_code": c.get("vendorCode"),
             "payload": psycopg2.extras.Json(c)}
            for c in cards if c.get("nmID") is not None]
    return db.upsert("raw_wb_card_content", recs, conflict_cols=["account", "nm_id"],
                     update_cols=["vendor_code", "payload", "collected_at"])


def main(account="wb_acc1"):
    print(f"WB контент карточек {account}", flush=True)
    cards = fetch(account)
    n = load_raw(account, cards)
    with_desc = sum(1 for c in cards if (c.get("description") or "").strip())
    with_char = sum(1 for c in cards if c.get("characteristics"))
    print(f"Записано карточек: {n} | с описанием: {with_desc} | с характеристиками: {with_char}",
          flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "wb_acc1")

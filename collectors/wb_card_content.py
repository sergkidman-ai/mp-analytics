"""collectors/wb_card_content.py — полный контент карточек WB (описание + характеристики) → raw.

Зачем: wb_cards несёт только заголовок+габариты. Для ответов на отзывы/вопросы нужен текст,
который видит покупатель — description и characteristics[] (чип, ресурс, совместимые модели).
Тот же эндпоинт content-api, что и wb.collect_cards, но кладём КАРТОЧКУ ЦЕЛИКОМ в
raw_wb_card_content. Разбор в фактические признаки — в reports/feedback_grounding.

Идемпотентно: UPSERT по (account, nm_id). Собираем ОБА аккаунта (wb_acc1 + wb_acc2): у Дисквэра
отзывов действительно нет, но ВОПРОСЫ есть (838 SKU с обращениями на 27.07) — а без контента карточки
домен-фильтр отправлял их на человека как «нет данных карточки».

Запуск:  ./venv/bin/python collectors/wb_card_content.py            # оба аккаунта
         ./venv/bin/python collectors/wb_card_content.py wb_acc2    # один
"""
import os
import sys
import time
import pathlib

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                              # noqa: E402
from collectors.wb import CARDS_URL, _token      # noqa: E402


CONTENT_TOKEN_ENV = {"wb_acc1": "WB_TOKEN_CONTENT_ACC1", "wb_acc2": "WB_TOKEN_CONTENT_ACC2"}


def _content_token(account):
    """Токен с категорией «Контент», с откатом на общий токен аккаунта.

    Базовые WB_TOKEN_ACC* охватывают статистику; на content-api у wb_acc2 они дают 401 (у wb_acc1
    исторически проходили). Отдельные WB_TOKEN_CONTENT_ACC* заведены потоком gab — читаем их,
    ничего не меняя на их территории."""
    return os.getenv(CONTENT_TOKEN_ENV.get(account, "")) or _token(account)


def fetch(account):
    """Все карточки постранично (курсор updatedAt+nmID). Возвращает список полных объектов."""
    H = {"Authorization": _content_token(account), "Content-Type": "application/json"}
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


ACCOUNTS = ("wb_acc1", "wb_acc2")


def main(account=None):
    """account=None → оба аккаунта. Возвращает суммарное число записанных карточек."""
    total = 0
    for acc in ((account,) if account else ACCOUNTS):
        print(f"WB контент карточек {acc}", flush=True)
        cards = fetch(acc)
        n = load_raw(acc, cards)
        with_desc = sum(1 for c in cards if (c.get("description") or "").strip())
        with_char = sum(1 for c in cards if c.get("characteristics"))
        print(f"Записано карточек: {n} | с описанием: {with_desc} | с характеристиками: {with_char}",
              flush=True)
        total += n
    return total


def refresh_if_stale():
    """Шаг цикла ответов: перезабрать контент, если он старше FEEDBACK_CARDS_MAX_AGE_DAYS (по умолч. 7).

    Гейт по возрасту, а не безусловный сбор: полный проход по обоим аккаунтам — это ~30k карточек и
    несколько минут, а цикл ответов ходит каждые 2 часа. Аккаунт без единой строки считается
    просроченным всегда (первый сбор). Возвращает список реально обновлённых аккаунтов."""
    max_age = int(os.environ.get("FEEDBACK_CARDS_MAX_AGE_DAYS", "7"))
    rows = db.query("""SELECT account, extract(epoch FROM now()-max(collected_at))/86400 AS age_days
                       FROM raw_wb_card_content GROUP BY account""")
    age = {r["account"]: float(r["age_days"]) for r in rows}
    stale = [a for a in ACCOUNTS if age.get(a) is None or age[a] > max_age]
    for acc in stale:
        main(acc)
    return stale


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)

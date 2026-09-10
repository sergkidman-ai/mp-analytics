"""collectors/ozon_attributes.py — характеристики/описание карточек Ozon → raw_ozon_attributes.

Зачем: ozon_product несёт только имя+флаг архива. Для ответов на отзывы/вопросы нужен текст
родной карточки — атрибуты (совместимые модели, тип, ресурс, чип) и аннотация, которые видит
покупатель. Источник: POST /v4/product/info/attributes (пагинация last_id). Кладём объект
целиком в raw_ozon_attributes по offer_id (= МС code, тот же ключ, что использует grounding).

Идемпотентно: UPSERT по (account, offer_id). ОБА аккаунта: до 10.09.2026 собирался только
Премиум (oz_acc1) «где есть отзывы» — но у Дисквэра (oz_acc2) отзывов действительно нет, а
ВОПРОСЫ есть (303 шт. на 222 карточки), и все они уходили в ИИ-слой с пустым CARD_DATA.

Запуск:  ./venv/bin/python collectors/ozon_attributes.py [oz_acc1|oz_acc2|all]
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


ACCOUNTS = ("oz_acc1", "oz_acc2")


def _alert(text, key):
    """Тревога в Telegram. Канал тот же, что у сторожей движка отзывов, с дедупом по ключу."""
    try:
        from feedback_bot.health import notify
        notify(text, key=key, repeat_hours=20)
    except Exception as e:                                    # noqa: BLE001
        print(f"АЛЕРТ НЕ ОТПРАВЛЕН ({e}): {text}", flush=True)


def _catalog_size(account):
    """Сколько живых карточек у аккаунта по нашим же данным — эталон для сверки."""
    r = db.query("""SELECT count(*) n FROM ozon_product
                    WHERE account=%s AND NOT coalesce(is_archived,false)""", (account,))
    return r[0]["n"] if r else 0


def check_after_run(account, got, with_attr):
    """Сторож от тихой дыры: у Дисквэра атрибуты не собирались с 25.08 по 10.09.2026, и заметили
    это только вручную — потому что «ничего не пришло» выглядело ровно как «нечего присылать».

    Условие тревоги: по аккаунту НОЛЬ карточек с атрибутами, а каталог у него непустой. Значит
    сломан не каталог, а наш путь к нему: ключ, права, эндпоинт. Возвращает текст тревоги или ''.
    """
    cat = _catalog_size(account)
    if with_attr or not cat:
        return ""
    return (f"⚠️ Атрибуты карточек Ozon {account}: получено {got} карточек, с атрибутами 0, "
            f"а в каталоге {cat} живых листингов.\n"
            f"Значит ответы на вопросы этого магазина идут без CARD_DATA. Проверить: ключ/права "
            f"аккаунта в .env, эндпоинт /v4/product/info/attributes.")


def main(account="all"):
    """account: конкретный аккаунт или "all" — оба (по умолчанию, чтобы новый аккаунт не
    оставался без карточек молча, как Дисквэр с 25.08 по 10.09.2026)."""
    bad = []
    for acc in (ACCOUNTS if account in ("all", None) else (account,)):
        print(f"Ozon атрибуты карточек {acc}", flush=True)
        try:
            items = fetch(acc)
        except Exception as e:                                # noqa: BLE001
            # Упавший прогон — та же дыра, только громче: без алерта он тоже пройдёт незамеченным.
            bad.append(acc)
            print(f"{acc}: ПРОГОН УПАЛ: {e}", flush=True)
            _alert(f"⚠️ Атрибуты карточек Ozon {acc}: прогон упал — {e}\n"
                   f"Карточки этого магазина в CARD_DATA не обновятся.", key=f"oz_attr_fail_{acc}")
            continue
        n = load_raw(acc, items)
        with_attr = sum(1 for it in items if it.get("attributes"))
        print(f"{acc}: записано карточек {n} | с атрибутами {with_attr}", flush=True)
        warn = check_after_run(acc, len(items), with_attr)
        if warn:
            bad.append(acc)
            print(f"{acc}: ТРЕВОГА — {warn}", flush=True)
            _alert(warn, key=f"oz_attr_empty_{acc}")
    return bad


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")

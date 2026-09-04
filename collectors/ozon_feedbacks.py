"""collectors/ozon_feedbacks.py — отзывы и вопросы Ozon → raw_feedback (для ответов).

Забираем ТОЛЬКО вопросы: POST /v1/question/list, пагинация last_id, is_answered =
answers_count>0. Отзывы по API закрыты подпиской на обоих юрлицах и не запрашиваются —
подробности в комментарии внутри collect(); отвечаем на них через браузер в ЛК.

Запуск:  ./venv/bin/python collectors/ozon_feedbacks.py [oz_acc1]
"""
import sys
import time
import pathlib

import requests
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402
from collectors.ozon import _headers         # noqa: E402

QUESTION_URL = "https://api-seller.ozon.ru/v1/question/list"


def _paginate(url, H, key, base_body):
    last_id = ""
    while True:
        body = dict(base_body)
        if last_id:
            body["last_id"] = last_id
        r = requests.post(url, headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        j = r.json()
        items = j.get(key) or []
        for it in items:
            yield it
        last_id = j.get("last_id") or ""
        if not items or not last_id or len(items) < base_body["limit"]:
            break
        time.sleep(0.25)


def _names(account):
    return {r["sku"]: r["name"] for r in
            db.query("SELECT sku, name FROM ozon_product WHERE account=%s", (account,))}


def main(account="oz_acc1"):
    H = _headers(account)
    print(f"Ozon вопросы {account}", flush=True)
    names = _names(account)
    recs, un_q = [], 0
    n_q = 0
    # Отзывы Ozon по API НЕ ЗАПРАШИВАЕМ ВООБЩЕ. Premium Plus не действует ни на одном юрлице
    # (oz_acc2 — всегда, oz_acc1 — с 23.08.2026), все /v1/review/* отдают 403
    # "not available with existing subscription". Решение: подписку не возвращаем, отзывы
    # читаются и отвечаются через браузер в ЛК (задача «ЛК Ozon» потока rev). Пробы методов —
    # docs/reports/08_ozon_acc2_reviews_api_2026-07-28.md, парковка непроходимых ответов —
    # флаг skipped_no_api (миграция 700). Запрос снят, чтобы не долбить 403 каждые полчаса
    # и не выдавать закрытый канал за поломку сбора.
    for qn in _paginate(QUESTION_URL, H, "questions", {"limit": 100}):
        answered = (qn.get("answers_count") or 0) > 0
        un_q += 0 if answered else 1
        n_q += 1
        recs.append({"platform": "ozon", "account": account, "kind": "question", "ext_id": str(qn["id"]),
                     "item_id": str(qn.get("sku") or ""), "article": None,
                     "product_name": names.get(str(qn.get("sku"))), "rating": None,
                     "body": qn.get("text") or "", "pros": None, "cons": None,
                     "created_at": qn.get("published_at"), "is_answered": answered,
                     "answer_text": None, "status": qn.get("status"), "payload": Json(qn)})
    cols = ["item_id", "article", "product_name", "rating", "body", "pros", "cons",
            "created_at", "is_answered", "answer_text", "status", "payload"]
    n = db.upsert("raw_feedback", recs,
                  conflict_cols=["platform", "account", "kind", "ext_id"], update_cols=cols)
    print(f"  отзывы: канал закрыт подпиской, ответы через ЛК | вопросов: {n_q} "
          f"(неотвеченных {un_q}) | записано {n}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")

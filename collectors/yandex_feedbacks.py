# поток: rev
"""collectors/yandex_feedbacks.py — отзывы Яндекс.Маркета → raw_feedback (для ответов).

Partner API: POST /businesses/{businessId}/goods-feedback (пагинация nextPageToken). У Яндекса
только ОТЗЫВЫ (вопросов в этом API нет). Признак «нужен ответ» — needReaction=true. Текст лежит
в description{comment/advantages/disadvantages}; рейтинг — statistics.rating; offerId=наш артикул
→ имя товара из ms_product. Голые оценки без текста (description={}) тоже сохраняем, но движок
ответов их отфильтрует (как пустые звёзды WB/Ozon).

Запуск:  ./venv/bin/python collectors/yandex_feedbacks.py
"""
import os
import sys
import time
import pathlib

import requests
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                 # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                            # noqa: E402

ACCOUNT = "ya_acc1"
API = "https://api.partner.market.yandex.ru"


def _cfg():
    return os.environ["YANDEX_API_KEY_ACC1"], os.environ["YANDEX_BUSINESS_ID_ACC1"]


def _pages(key, biz, limit=50):
    """Пагинация goods-feedback по nextPageToken."""
    H = {"Api-Key": key, "Content-Type": "application/json"}
    token = None
    while True:
        url = f"{API}/businesses/{biz}/goods-feedback"
        params = {"limit": limit}
        if token:
            params["page_token"] = token
        r = requests.post(url, headers=H, json={"limit": limit}, params=params, timeout=60)
        if r.status_code == 420 or r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        r.raise_for_status()
        res = r.json().get("result", {})
        fbs = res.get("feedbacks") or []
        for f in fbs:
            yield f
        token = (res.get("paging") or {}).get("nextPageToken")
        if not fbs or not token:
            break
        time.sleep(0.3)


def _names():
    return {r["article"]: r["name"] for r in
            db.query("SELECT article, name FROM ms_product WHERE article IS NOT NULL")}


def _text_parts(desc):
    """Из description собираем body(comment)/pros(advantages)/cons(disadvantages)."""
    if not isinstance(desc, dict):
        return "", None, None
    body = (desc.get("comment") or "").strip()
    pros = (desc.get("advantages") or "").strip() or None
    cons = (desc.get("disadvantages") or "").strip() or None
    return body, pros, cons


def main():
    key, biz = _cfg()
    print(f"Яндекс отзывы {ACCOUNT}", flush=True)
    names = _names()
    recs, un = [], 0
    n = 0
    for f in _pages(key, biz):
        st = f.get("statistics") or {}
        body, pros, cons = _text_parts(f.get("description"))
        # ответ продавца = наличие комментария (needReaction на этом аккаунте всегда False, не флаг)
        answered = (st.get("commentsCount") or 0) > 0
        offer = str((f.get("identifiers") or {}).get("offerId") or "")
        n += 1
        un += 0 if answered else 1
        recs.append({"platform": "yandex", "account": ACCOUNT, "kind": "review",
                     "ext_id": str(f["feedbackId"]), "item_id": offer, "article": offer or None,
                     "product_name": names.get(offer), "rating": st.get("rating"),
                     "body": body, "pros": pros, "cons": cons,
                     "created_at": f.get("createdAt"),
                     "is_answered": answered, "answer_text": None,
                     "status": None, "payload": Json(f)})
    cols = ["item_id", "article", "product_name", "rating", "body", "pros", "cons",
            "created_at", "is_answered", "answer_text", "status", "payload"]
    w = db.upsert("raw_feedback", recs,
                  conflict_cols=["platform", "account", "kind", "ext_id"], update_cols=cols)
    print(f"  отзывов: {n} (нужен ответ {un}) | записано {w}", flush=True)


if __name__ == "__main__":
    main()

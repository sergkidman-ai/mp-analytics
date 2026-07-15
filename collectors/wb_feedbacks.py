"""collectors/wb_feedbacks.py — отзывы и вопросы WB → raw_feedback (для ответов).

feedbacks-api.wildberries.ru: GET /api/v1/feedbacks (отзывы), /api/v1/questions (вопросы).
Токен acc1 («Цифровой квадрат») имеет скоуп «Вопросы и отзывы». У отзыва текст часто пуст —
содержание в pros/cons. Тянем оба состояния (отвеченные и нет), храним сырьё целиком.

Запуск:  ./venv/bin/python collectors/wb_feedbacks.py [wb_acc1]
"""
import os
import sys
import time
import pathlib

import requests
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402

BASE = "https://feedbacks-api.wildberries.ru/api/v1"
_TOKENS = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}


def _get(path, token, params):
    for _ in range(5):
        r = requests.get(f"{BASE}{path}", headers={"Authorization": token}, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(2)
            continue
        r.raise_for_status()
        return r.json()["data"]
    raise RuntimeError(f"WB {path}: too many retries")


def _pages(path, token):
    """Оба состояния (не/отвеченные), пагинация take/skip."""
    for answered in ("false", "true"):
        skip, take = 0, 1000
        while True:
            data = _get(path, token, {"isAnswered": answered, "take": take, "skip": skip, "order": "dateDesc"})
            items = (data or {}).get("feedbacks" if "feedback" in path else "questions") or []
            for it in items:
                yield it
            if len(items) < take:
                break
            skip += take
            time.sleep(0.3)


def _feedback_rec(account, it):
    pd = it.get("productDetails") or {}
    ans = it.get("answer")
    return {"platform": "wb", "account": account, "kind": "review", "ext_id": it["id"],
            "item_id": str(pd.get("nmId") or ""), "article": pd.get("supplierArticle"),
            "product_name": pd.get("productName"), "rating": it.get("productValuation"),
            "body": it.get("text") or "", "pros": it.get("pros"), "cons": it.get("cons"),
            "created_at": it.get("createdDate"), "is_answered": bool(ans),
            "answer_text": (ans or {}).get("text"), "status": it.get("state"), "payload": Json(it)}


def _question_rec(account, it):
    pd = it.get("productDetails") or {}
    ans = it.get("answer")
    return {"platform": "wb", "account": account, "kind": "question", "ext_id": it["id"],
            "item_id": str(pd.get("nmId") or ""), "article": pd.get("supplierArticle"),
            "product_name": pd.get("productName"), "rating": None,
            "body": it.get("text") or "", "pros": None, "cons": None,
            "created_at": it.get("createdDate"), "is_answered": bool(ans),
            "answer_text": (ans or {}).get("text"), "status": it.get("state"), "payload": Json(it)}


_COLS = ["item_id", "article", "product_name", "rating", "body", "pros", "cons",
         "created_at", "is_answered", "answer_text", "status", "payload"]


def main(account="wb_acc1"):
    token = os.environ[_TOKENS[account]]
    print(f"WB отзывы+вопросы {account}", flush=True)
    fb = [_feedback_rec(account, it) for it in _pages("/feedbacks", token)]
    q = [_question_rec(account, it) for it in _pages("/questions", token)]
    recs = fb + q
    n = db.upsert("raw_feedback", recs,
                  conflict_cols=["platform", "account", "kind", "ext_id"], update_cols=_COLS)
    un_fb = sum(1 for r in fb if not r["is_answered"])
    un_q = sum(1 for r in q if not r["is_answered"])
    print(f"  отзывов: {len(fb)} (неотвеченных {un_fb}) | вопросов: {len(q)} (неотвеченных {un_q}) | записано {n}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "wb_acc1")

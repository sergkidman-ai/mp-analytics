"""collectors/ozon_review_answers.py — тексты НАШИХ ответов на отзывы Ozon → raw_feedback.answer_text.

Список отзывов (/v1/review/list) текст ответа продавца НЕ отдаёт. Тянем его отдельно:
/v1/review/comment/list по каждому отзыву, берём комментарий с is_owner=true. Нужен, чтобы
Ozon-ответы вошли в RAG-справочник [[reports/feedback_corpus]]. Резюмируемо: пропускаем те,
где answer_text уже заполнен.

Запуск:  ./venv/bin/python collectors/ozon_review_answers.py [oz_acc1] [limit]
"""
import sys
import time
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402
from collectors.ozon import _headers         # noqa: E402

URL = "https://api-seller.ozon.ru/v1/review/comment/list"


def _owner_answer(H, review_id):
    for _ in range(4):
        r = requests.post(URL, headers=H, json={"review_id": review_id, "limit": 100,
                                                 "offset": 0, "sort_dir": "ASC"}, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        if r.status_code != 200:
            return None
        owner = [c.get("text") for c in r.json().get("comments", []) if c.get("is_owner")]
        return next((t for t in owner if t and t.strip()), None)
    return None


def main(account="oz_acc1", limit=None):
    H = _headers(account)
    rows = db.query("""SELECT ext_id FROM raw_feedback
        WHERE platform='ozon' AND account=%s AND kind='review' AND is_answered
          AND (answer_text IS NULL OR length(trim(answer_text))=0)
          AND length(trim(body))>0
        ORDER BY created_at DESC""", (account,))
    if limit:
        rows = rows[:limit]
    print(f"Ozon ответы на отзывы {account}: к добору {len(rows)}", flush=True)
    got = miss = 0
    for i, r in enumerate(rows, 1):
        ans = _owner_answer(H, r["ext_id"])
        if ans:
            db.execute("UPDATE raw_feedback SET answer_text=%s WHERE platform='ozon' AND account=%s "
                       "AND kind='review' AND ext_id=%s", (ans, account, r["ext_id"]))
            got += 1
        else:
            miss += 1
        if i % 250 == 0:
            print(f"  {i}/{len(rows)} — с ответом {got}, без {miss}", flush=True)
        time.sleep(0.15)
    print(f"Готово: заполнено answer_text {got}, без ответа {miss}", flush=True)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "oz_acc1"
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
    main(acc, lim)

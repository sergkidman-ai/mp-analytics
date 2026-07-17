"""collectors/ozon_question_answers.py — тексты НАШИХ ответов на ВОПРОСЫ Ozon → raw_feedback.answer_text.

Список вопросов (/v1/question/list) текст ответа продавца НЕ отдаёт (только answers_count).
Тянем его отдельно: /v1/question/answer/list по каждому вопросу, берём ответ, где author_name —
наш магазин («Цифровой квадрат»). Нужен, чтобы Ozon Q&A вошли в корпус прошлых ответов
[[reports/feedback_corpus]] как источник №1 для движка вопросов. Идемпотентно: пропускаем те,
где answer_text уже заполнен.

Запуск:  ./venv/bin/python collectors/ozon_question_answers.py [oz_acc1] [limit]
"""
import sys
import time
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402
from collectors.ozon import _headers         # noqa: E402

URL = "https://api-seller.ozon.ru/v1/question/answer/list"
OUR = "цифров"          # наш продавец — author_name «Цифровой квадрат»


def _owner_answer(H, question_id, sku):
    body = {"question_id": question_id, "limit": 100, "offset": 0}
    if sku:
        try:
            body["sku"] = int(sku)
        except (TypeError, ValueError):
            pass
    for _ in range(4):
        r = requests.post(URL, headers=H, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        if r.status_code != 200:
            return None
        ours = [a.get("text") for a in r.json().get("answers", [])
                if OUR in (a.get("author_name") or "").lower()]
        return next((t for t in ours if t and t.strip()), None)
    return None


def main(account="oz_acc1", limit=None):
    H = _headers(account)
    rows = db.query("""SELECT ext_id, payload->>'id' qid, payload->>'sku' sku FROM raw_feedback
        WHERE platform='ozon' AND account=%s AND kind='question' AND is_answered
          AND (answer_text IS NULL OR length(trim(answer_text))=0)
          AND (payload->>'answers_count')::int > 0
        ORDER BY created_at DESC""", (account,))
    if limit:
        rows = rows[:limit]
    print(f"Ozon ответы на вопросы {account}: к добору {len(rows)}", flush=True)
    got = miss = 0
    for i, r in enumerate(rows, 1):
        ans = _owner_answer(H, r["qid"], r["sku"])
        if ans:
            db.execute("UPDATE raw_feedback SET answer_text=%s WHERE platform='ozon' AND account=%s "
                       "AND kind='question' AND ext_id=%s", (ans, account, r["ext_id"]))
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

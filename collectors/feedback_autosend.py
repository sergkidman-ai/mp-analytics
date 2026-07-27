# поток: rev
"""collectors/feedback_autosend.py — АВТООТПРАВКА позитив-шаблонов отзывов (draft_route='auto').

Единственный источник auto-маршрута — пустые ★5 отзывы без текста (детерминированный шаблон,
reports/feedback_today.py). Вопросы и любой отзыв с текстом/жалобой сюда не попадают (route='review',
идут через модерацию Telegram). Пейсинг: максимум FEEDBACK_AUTOSEND_BATCH (default 5) на канал за
цикл, пауза 3-5 сек между отправками внутри канала, свежие сначала (ORDER BY created_at DESC —
одновременно закрывает и «сначала свежие», и «затем старые от новых к старым», весь бэклог
капельно по вызовам). Отправка — через collectors.feedback_send.post_answer (единый choke point:
FEEDBACK_LIVE_SEND + FEEDBACK_LIVE_ACCOUNTS + FEEDBACK_DAILY_SEND_CAP применяются автоматически).

Запуск:  ./venv/bin/python collectors/feedback_autosend.py
"""
import os
import sys
import time
import random
import pathlib
from collections import defaultdict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                          # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                      # noqa: E402
from collectors import feedback_send as fs               # noqa: E402

BATCH = int(os.environ.get("FEEDBACK_AUTOSEND_BATCH", "5"))


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] feedback_autosend: {msg}", flush=True)


def _candidates():
    rows = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,payload,
        draft_text,created_at FROM raw_feedback
        WHERE draft_route='auto' AND is_answered=false AND posted_at IS NULL
        AND draft_text IS NOT NULL AND NOT skipped_old
        ORDER BY created_at DESC""")
    by_channel = defaultdict(list)
    for r in rows:
        by_channel[(r["platform"], r["account"])].append(r)
    return by_channel


def run():
    by_channel = _candidates()
    total_sent, total_fail = 0, 0
    if not by_channel:
        _log("нечего отправлять (пусто в draft_route=auto)")
        return {"sent": 0, "fail": 0, "channels": 0}

    for (plat, acc), rows in by_channel.items():
        portion = rows[:BATCH]
        _log(f"{plat}/{acc}: {len(rows)} в очереди, отправляю порцию {len(portion)}")
        for i, r in enumerate(portion):
            ok, detail = fs.post_answer(dict(r), r["draft_text"])
            if ok and detail == "sent":
                total_sent += 1
            elif not ok:
                total_fail += 1
                _log(f"  ОШИБКА {plat}/{acc} {r['kind']}={r['ext_id']}: {detail}")
            if i < len(portion) - 1:
                time.sleep(random.uniform(3, 5))

    _log(f"итог: отправлено {total_sent}, ошибок {total_fail}, каналов затронуто {len(by_channel)}")
    return {"sent": total_sent, "fail": total_fail, "channels": len(by_channel)}


if __name__ == "__main__":
    run()

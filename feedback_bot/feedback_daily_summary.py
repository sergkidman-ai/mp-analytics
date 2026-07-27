# поток: rev
"""feedback_bot/feedback_daily_summary.py — суточная сводка в Telegram (systemd-таймер раз в сутки).

Отправлено за сутки / опубликовано после модерации / в очереди / ошибок отправки / потрачено на LLM.
Считает по calendar-дню "вчера" (таймер срабатывает утром — за прошедшие полные сутки).

Запуск:  ./venv/bin/python feedback_bot/feedback_daily_summary.py
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                          # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                      # noqa: E402
from feedback_bot import tg_moderation as tm             # noqa: E402


def _counts():
    sent = db.query("""SELECT count(*) AS n FROM raw_feedback
        WHERE posted_ok=true AND posted_at::date = current_date - 1""")[0]["n"]
    published_mod = db.query("""SELECT count(*) AS n FROM feedback_moderation
        WHERE state='sent' AND decided_at::date = current_date - 1""")[0]["n"]
    queued = db.query("""SELECT count(*) AS n FROM feedback_moderation
        WHERE state IN ('queued', 'carded', 'snoozed')""")[0]["n"]
    errors = db.query("""SELECT
        (SELECT count(*) FROM raw_feedback WHERE posted_ok=false AND posted_at::date = current_date - 1)
      + (SELECT count(*) FROM feedback_moderation WHERE state='failed' AND decided_at::date = current_date - 1)
        AS n""")[0]["n"]
    cost = db.query("""SELECT coalesce(sum(cost_usd), 0) AS usd, coalesce(sum(calls), 0) AS calls
        FROM feedback_llm_cost_log WHERE day = current_date - 1""")[0]
    return {"sent": sent, "published_mod": published_mod, "queued": queued,
            "errors": errors, "cost_usd": float(cost["usd"]), "llm_calls": cost["calls"]}


def build_text(c):
    return ("📊 <b>Сводка по отзывам/вопросам за сутки</b>\n\n"
            f"Отправлено (авто+модерация): <b>{c['sent']}</b>\n"
            f"Опубликовано после ✅ модерации: <b>{c['published_mod']}</b>\n"
            f"В очереди модерации сейчас: <b>{c['queued']}</b>\n"
            f"Ошибок отправки: <b>{c['errors']}</b>\n"
            f"Потрачено на LLM: <b>${c['cost_usd']:.4f}</b> ({c['llm_calls']} вызовов)")


def main():
    c = _counts()
    text = build_text(c)
    print(text.replace("<b>", "").replace("</b>", ""), flush=True)
    for cid in tm.NOTIFY_IDS:
        tm.send(cid, text)


if __name__ == "__main__":
    main()

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


# Сутки считаем по КАЛЕНДАРЮ МОСКВЫ: таймзона БД = Etc/UTC, и `::date = current_date - 1` резал
# сутки по 03:00 МСК — в «вчера» попадал кусок позавчера, а вечер (21:00–24:00 МСК) уезжал в «сегодня».
_MSK_YDAY = "(now() AT TIME ZONE 'Europe/Moscow')::date - 1"
_MSK_TODAY = "(now() AT TIME ZONE 'Europe/Moscow')::date"


def _counts():
    sent = db.query(f"""SELECT count(*) AS n FROM raw_feedback
        WHERE posted_ok=true AND (posted_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY}""")[0]["n"]
    published_mod = db.query(f"""SELECT count(*) AS n FROM feedback_moderation
        WHERE state='sent' AND (decided_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY}""")[0]["n"]
    queued = db.query("""SELECT count(*) AS n FROM feedback_moderation
        WHERE state IN ('queued', 'carded', 'snoozed')""")[0]["n"]
    # 'deferred' — хвост СТАРОЙ схемы лимита (когда лимит резал и ручные ответы). Новые карточки сюда
    # не попадают; остаток цикл дошлёт сам (tg_moderation.flush_deferred), строку показываем пока он есть.
    deferred = db.query("""SELECT count(*) AS n FROM feedback_moderation
        WHERE state='deferred'""")[0]["n"]
    errors = db.query(f"""SELECT
        (SELECT count(*) FROM raw_feedback WHERE posted_ok=false
            AND (posted_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY})
      + (SELECT count(*) FROM feedback_moderation WHERE state='failed'
            AND (decided_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY})
        AS n""")[0]["n"]
    cost = db.query(f"""SELECT coalesce(sum(cost_usd), 0) AS usd, coalesce(sum(calls), 0) AS calls
        FROM feedback_llm_cost_log WHERE day = {_MSK_YDAY}""")[0]
    # ФАКТ по каналам: за отчётные сутки и за сегодня (день сводки ещё идёт) — видно не только
    # остаток лимита, но и сколько ответов реально ушло покупателям.
    by_yday = db.query(f"""SELECT platform, account, count(*) AS n FROM raw_feedback
        WHERE posted_ok=true AND (posted_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY}
        GROUP BY 1,2 ORDER BY 1,2""")
    by_today = db.query(f"""SELECT platform, account, count(*) AS n FROM raw_feedback
        WHERE posted_ok=true AND (posted_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_TODAY}
        GROUP BY 1,2 ORDER BY 1,2""")
    return {"sent": sent, "published_mod": published_mod, "queued": queued, "deferred": deferred,
            "errors": errors, "cost_usd": float(cost["usd"]), "llm_calls": cost["calls"],
            "by_yday": by_yday, "by_today": by_today}


def _chan_lines(rows):
    return "\n".join(f"  • {r['platform']} ({r['account']}): <b>{r['n']}</b>" for r in rows) \
        or "  • нет публикаций"


def build_text(c):
    return ("📊 <b>Сводка по отзывам/вопросам за сутки</b> (календарь МСК)\n\n"
            f"Опубликовано за сутки (авто+модерация): <b>{c['sent']}</b>\n"
            f"{_chan_lines(c['by_yday'])}\n"
            f"Опубликовано сегодня (с 00:00 МСК): <b>{sum(r['n'] for r in c['by_today'])}</b>\n"
            f"{_chan_lines(c['by_today'])}\n\n"
            f"Из них после ✅ модерации: <b>{c['published_mod']}</b>\n"
            f"В очереди модерации сейчас: <b>{c['queued']}</b>\n"
            + (f"Хвост старой схемы лимита (дошлётся сам): <b>{c['deferred']}</b>\n"
               if c["deferred"] else "")
            + f"Ошибок отправки: <b>{c['errors']}</b>\n"
            f"Потрачено на LLM: <b>${c['cost_usd']:.4f}</b> ({c['llm_calls']} вызовов)")


def main():
    c = _counts()
    text = build_text(c)
    print(text.replace("<b>", "").replace("</b>", ""), flush=True)
    for cid in tm.NOTIFY_IDS:
        tm.send(cid, text)


if __name__ == "__main__":
    main()

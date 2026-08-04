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
    # Ошибки отправки БЕЗ застрявших: разовый сбой (повтор будет) и «повторы прекращены» — разные
    # вещи, второе требует ручного вмешательства и не должно тонуть в общем счётчике.
    errors = db.query(f"""SELECT
        (SELECT count(*) FROM raw_feedback f WHERE f.posted_ok=false
            AND (f.posted_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY}
            AND NOT EXISTS (SELECT 1 FROM feedback_send_attempts a
                WHERE (a.platform,a.account,a.kind,a.ext_id)=(f.platform,f.account,f.kind,f.ext_id)
                  AND a.blocked))
      + (SELECT count(*) FROM feedback_moderation m WHERE m.state='failed'
            AND (m.decided_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY}
            AND NOT EXISTS (SELECT 1 FROM feedback_send_attempts a
                WHERE (a.platform,a.account,a.kind,a.ext_id)=(m.platform,m.account,m.kind,m.ext_id)
                  AND a.blocked))
        AS n""")[0]["n"]
    # Застрявшие: повторы прекращены после FEEDBACK_SEND_MAX_ATTEMPTS неудач — отдельной строкой.
    stuck_yday = db.query(f"""SELECT count(*) AS n FROM feedback_send_attempts
        WHERE blocked AND (last_at AT TIME ZONE 'Europe/Moscow')::date = {_MSK_YDAY}""")[0]["n"]
    stuck_open = db.query("""SELECT platform, account, kind, ext_id, attempts, last_error
        FROM feedback_send_attempts WHERE blocked ORDER BY last_at DESC""")
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
            "by_yday": by_yday, "by_today": by_today,
            "stuck_yday": stuck_yday, "stuck_open": stuck_open, "index": _index()}


def _index():
    """Паспорт индекса совместимости (подбор товара по модели принтера). Своя таблица и свой
    сбойный путь — если он недоступен, сводка всё равно должна уйти."""
    try:
        from reports import compat_index
        return compat_index.meta()
    except Exception:
        return None


def _index_line(c):
    """Возраст индекса подбора — ОТДЕЛЬНОЙ строкой: протухший индекс не ломает ответы, он молча
    перестаёт находить новые карточки, и без этой строки это видно только раскопками в логах.
    Порог тревоги — двое суток (гейт пересборки в цикле — 24 ч)."""
    m = c.get("index")
    if not m:
        return "⚠️ Индекс подбора: <b>НЕ СОБРАН</b> (подбор идёт запасным путём)\n"
    age = m["age_hours"]
    mark = "⚠️ " if age > 48 else ""
    age_txt = f"{age:.0f} ч" if age < 48 else f"<b>{age / 24:.1f} сут</b>"
    return (f"{mark}Индекс подбора: обновлён {age_txt} назад · моделей <b>{m['models_total']}</b> · "
            f"листингов {m['items_total']}\n")


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
            + f"Ошибок отправки (разовые, повтор будет): <b>{c['errors']}</b>\n"
            + _stuck_line(c)
            + _index_line(c)
            + f"Потрачено на LLM: <b>${c['cost_usd']:.4f}</b> ({c['llm_calls']} вызовов)")


def _stuck_line(c):
    """⛔ Застрявшие отправки — ОТДЕЛЬНОЙ строкой, не в общем счётчике ошибок: повторы по ним
    прекращены и без человека они не уйдут никогда."""
    if not c["stuck_open"] and not c["stuck_yday"]:
        return ""
    head = (f"⛔ <b>Застряло (повторы прекращены): {len(c['stuck_open'])}</b>"
            + (f" · за отчётные сутки: {c['stuck_yday']}" if c["stuck_yday"] else "") + "\n")
    body = ""
    for r in c["stuck_open"][:5]:
        err = (r["last_error"] or "")[:90].replace("<", "&lt;").replace(">", "&gt;")
        body += (f"  • {r['platform']}/{r['account']} {r['kind']}=<code>{r['ext_id']}</code> "
                 f"({r['attempts']} поп.): {err}\n")
    if len(c["stuck_open"]) > 5:
        body += f"  • …и ещё {len(c['stuck_open']) - 5}\n"
    return head + body


def main():
    c = _counts()
    text = build_text(c)
    print(text.replace("<b>", "").replace("</b>", ""), flush=True)
    for cid in tm.NOTIFY_IDS:
        tm.send(cid, text)


if __name__ == "__main__":
    main()

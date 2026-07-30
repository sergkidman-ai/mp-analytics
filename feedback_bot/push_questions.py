# поток: rev
"""feedback_bot/push_questions.py — разовый прогон «все неотвеченные ВОПРОСЫ за N дней в модерацию».

Обычный цикл держит вопросы в окне 30 дней (`FEEDBACK_DRAFT_SINCE_DAYS`) и всё, что старше,
помечает `skipped_old=true`. Когда нужно разово поднять более широкое окно (90 дней и т.п.),
этот скрипт:
  1. снимает `skipped_old` с вопросов ВНУТРИ запрошенного окна (вне окна флаг не трогаем);
  2. генерирует черновики тем, у кого их нет или изменился текст (движок reports/feedback_today);
  3. ставит в очередь модерации;
  4. рассылает карточки в Telegram — БЕЗ порционного лимита цикла (лимит только у авто-ответов
     на бэклог отзывов; вопросы им не режутся).

Отправка ответов площадке отсюда НЕ идёт: вопросы всегда route=review, уходят только по ✅ оператора.

Запуск:  ./venv/bin/python feedback_bot/push_questions.py --days 90 [--limit N] [--dry]
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                 # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                            # noqa: E402


def unskip(days):
    """Вернуть в работу вопросы внутри окна, ранее помеченные skipped_old."""
    return db.execute("""UPDATE raw_feedback SET skipped_old=false
        WHERE kind='question' AND skipped_old AND is_answered=false AND posted_at IS NULL
          AND created_at >= now() - make_interval(days => %s)""", (days,))


def drafts(days):
    """Черновики для вопросов окна: только те, где черновика нет или текст вопроса изменился."""
    from reports import feedback_today as ft
    rows = db.query("""SELECT platform, account, kind, ext_id, item_id, article, product_name,
            rating, body, pros, cons, created_at
        FROM raw_feedback
        WHERE kind='question' AND is_answered=false AND posted_at IS NULL AND NOT skipped_old
          AND created_at >= now() - make_interval(days => %s)
          AND (draft_text IS NULL
               OR draft_src_hash IS DISTINCT FROM md5(coalesce(body,'')||coalesce(pros,'')||coalesce(cons,'')))
        ORDER BY created_at DESC""", (days,))
    if not rows:
        return 0
    cf, corpus, client = ft.CardFacts(), ft.load_corpus(), ft._client()
    for r in rows:
        r = dict(r)
        _, reply, route, conf, ground, _, _ = ft._answer(client, r, cf, corpus)
        ft._store(r, reply, route, conf, ground)
        ft._enqueue_moderation(r, reply)
    return len(rows)


def enqueue(days):
    """Догнать очередь: вопросы с черновиком, которых ещё нет в feedback_moderation."""
    from reports import feedback_today as ft
    rows = db.query("""SELECT f.platform, f.account, f.kind, f.ext_id, f.item_id, f.product_name,
            f.body, f.draft_text
        FROM raw_feedback f
        LEFT JOIN feedback_moderation m ON (m.platform, m.account, m.kind, m.ext_id)
                                        = (f.platform, f.account, f.kind, f.ext_id)
        WHERE f.kind='question' AND f.is_answered=false AND f.posted_at IS NULL AND NOT f.skipped_old
          AND f.draft_text IS NOT NULL AND m.id IS NULL
          AND f.created_at >= now() - make_interval(days => %s)""", (days,))
    for r in rows:
        ft._enqueue_moderation(dict(r), r["draft_text"])
    return len(rows)


def main(days=90, limit=200, dry=False):
    un = unskip(days)
    d = drafts(days)
    q = enqueue(days)
    pend = db.query("""SELECT m.platform, m.account, count(*) n
        FROM feedback_moderation m JOIN raw_feedback f
          ON (f.platform,f.account,f.kind,f.ext_id)=(m.platform,m.account,m.kind,m.ext_id)
        WHERE m.kind='question' AND (m.state='queued' AND m.tg_msg_id IS NULL
              OR m.state='snoozed' AND m.snooze_until <= now())
          AND COALESCE(f.is_answered,false)=false AND f.posted_at IS NULL
          AND f.created_at >= now() - make_interval(days => %s)
        GROUP BY 1,2 ORDER BY 1,2""", (days,))
    print(f"окно {days} дн.: снят skipped_old {un}, черновиков сгенерено {d}, поставлено в очередь {q}")
    print("к показу:", {f"{r['platform']}/{r['account']}": r["n"] for r in pend} or "пусто")
    if dry:
        return 0
    from feedback_bot import tg_moderation
    sent = tg_moderation.send_batch(limit=limit, days=days, kind="question")
    print(f"карточек отправлено в Telegram: {sent}")
    return sent


if __name__ == "__main__":
    a = sys.argv[1:]
    kw = {"days": int(a[a.index("--days") + 1]) if "--days" in a else 90,
          "limit": int(a[a.index("--limit") + 1]) if "--limit" in a else 200,
          "dry": "--dry" in a}
    main(**kw)

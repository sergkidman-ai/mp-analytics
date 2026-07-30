# поток: rev
"""feedback_bot/backfill_review_drafts.py — разовая генерация черновиков ОТЗЫВОВ за широкое окно.

Обычный цикл драфтит только окно `run(since=...)` (последний месяц), поэтому отзывы старше окна
остаются без `draft_text` и не видны ни авто-отправке, ни модерации — висят мёртвым грузом
(на 28.07 таких было 767 у wb_acc2, самый старый от 22.07.2025). Скрипт разово догоняет их:
берёт отзывы без черновика (или с устаревшим `draft_src_hash`) в заданном окне и по заданным
аккаунтам, гоняет тот же движок `reports/feedback_today._answer` и складывает результат.

Что делает и чего НЕ делает:
  * генерирует черновик и кладёт в `raw_feedback` (`_store`);
  * отзывы С ТЕКСТОМ ставит в очередь модерации (`_enqueue_moderation`) — уйдут только по ✅ оператора;
  * пустые оценки-звёзды получают позитив-шаблон `draft_route='auto'` — их подхватит штатная
    авто-отправка `collectors/feedback_autosend.py` в рамках дневного лимита канала;
  * САМ НИЧЕГО НА ПЛОЩАДКУ НЕ ОТПРАВЛЯЕТ — ни одного вызова API маркетплейса здесь нет.

Запуск:  ./venv/bin/python feedback_bot/backfill_review_drafts.py --account wb_acc2 --days 400 [--limit N] [--dry]
"""
import sys
import pathlib
from collections import Counter

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                 # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                            # noqa: E402


def pending(accounts, days, limit=None):
    """Отзывы окна без черновика (или с устаревшим хэшем текста). Фильтры — как в _gather()."""
    sql = """SELECT platform,account,kind,ext_id,item_id,article,product_name,rating,body,pros,cons,
            payload,created_at
        FROM raw_feedback
        WHERE kind='review' AND is_answered=false AND posted_at IS NULL
          AND NOT skipped_old AND NOT skipped_no_text
          AND account = ANY(%s)
          AND created_at >= now() - make_interval(days => %s)
          AND (draft_text IS NULL
               OR draft_src_hash IS DISTINCT FROM md5(coalesce(body,'')||coalesce(pros,'')||coalesce(cons,'')))
        ORDER BY created_at DESC"""
    params = [list(accounts), days]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return db.query(sql, tuple(params))


def main(accounts, days=400, limit=None, dry=False):
    rows = pending(accounts, days, limit)
    c_in = Counter(("с текстом" if (r["body"] or r["pros"] or r["cons"] or "").strip() else "звёзды")
                   for r in rows)
    print(f"аккаунты {','.join(accounts)}, окно {days} дн.: к генерации {len(rows)} "
          f"({dict(c_in)})", flush=True)
    if dry or not rows:
        return 0

    from reports import feedback_today as ft
    cf, corpus, client = ft.CardFacts(), ft.load_corpus(), ft._client()
    routes, done = Counter(), 0
    for i, r in enumerate(rows, 1):
        r = dict(r)
        _, reply, route, conf, ground, _, _ = ft._answer(client, r, cf, corpus)
        ft._store(r, reply, route, conf, ground)
        ft._enqueue_moderation(r, reply)
        routes[route] += 1
        done += 1
        if i % 100 == 0 or i == len(rows):
            print(f"  [{i}/{len(rows)}] {dict(routes)}", flush=True)
    print(f"готово: черновиков {done}, маршруты {dict(routes)}", flush=True)
    print("стоимость ИИ за прогон:", ft._COST.summary(), flush=True)
    ft._COST.persist()
    return done


if __name__ == "__main__":
    a = sys.argv[1:]
    accs = a[a.index("--account") + 1].split(",") if "--account" in a else ["wb_acc2"]
    main(accounts=accs,
         days=int(a[a.index("--days") + 1]) if "--days" in a else 400,
         limit=int(a[a.index("--limit") + 1]) if "--limit" in a else None,
         dry="--dry" in a)

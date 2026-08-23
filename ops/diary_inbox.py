#!/usr/bin/env python3
# поток: ev
"""Приёмник записок из dropbox-бота (@Pro_Dropbox_bot) в дневник событий.

Зачем. Наталья бросает в бот короткие записки — «сегодня БПЛА по складу Оренбург РФЦ»,
«Маркет меняет тариф хранения». До 23.08.2026 их вручную переносила в дневник сессия: пока
сессии нет, событие лежит в файле и в отчётах не видно. Приёмник закрывает разрыв: записка
попадает в дневник сама, в тот же день.

Что берём и чего НЕ берём. Только ЗАМЕТКИ (`*_note.txt`). Файлы, скриншоты и подписи к ним
(`*.caption.txt`, .jpg, .xlsx) в бот бросают для работы сессий — прайсы, выгрузки, картинки
на разбор; в дневник они не идут (решение Сергея 23.08.2026).

Чего приёмник НЕ делает — не угадывает. Площадка ставится, только если названа словом
(«Ozon», «ВБ», «Маркет»); не названа — событие без площадки, а не «наверное, Озон»: неверная
площадка сажает запись на чужой график. Вид события: назвали площадку → `mp` (новость
площадки), нет → `own` (наше решение/заметка). Всё принятое помечается 📥 «из бота, не
разобрано» — по значку в ленте видно, что запись пришла сырой, её стоит дочитать и поправить
(правка записок разрешена: `biz_diary.update`).

Идемпотентность — журнал `diary_inbox` (миграция 510), ключ = имя файла. Сознательный отказ
(«поручение, а не событие») тоже пишется в журнал, иначе следующий проход завёл бы запись
снова. Файлы в dropbox живут 7 дней (invoice_bot/cleanup_inbox.py) — журнал переживает уборку.

    ./venv/bin/python -m ops.diary_inbox --dry          # показать, что завёл бы
    ./venv/bin/python -m ops.diary_inbox                # боевой прогон (в кроне)
    ./venv/bin/python -m ops.diary_inbox --seed-known   # пометить всё лежащее как разобранное
    ./venv/bin/python -m ops.diary_inbox --skip ФАЙЛ --why "поручение, не событие"
    ./venv/bin/python -m ops.diary_inbox --list
"""
import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, "/opt/mp-analytics")

from core import db                      # noqa: E402
from ops import biz_diary                # noqa: E402

DROPBOX = "/opt/mp-analytics/dropbox"
NOTE_SUFFIX = "_note.txt"
MARK = "📥"

# Автор — из имени файла (…_Natalie_Pro_natalya_note.txt). Кто именно написал, важно:
# в дневнике «Наталья» и «Сергей» — разные голоса, а «natalya» в ленте выглядит мусором.
AUTHORS = {"natalya": "Наталья", "natalie": "Наталья", "sergey": "Сергей", "serg": "Сергей"}


def notes_on_disk():
    """Заметки в ящике, старые первыми. Медиа и подписи к ним не берём."""
    if not os.path.isdir(DROPBOX):
        return []
    return sorted(f for f in os.listdir(DROPBOX) if f.endswith(NOTE_SUFFIX))


def known():
    return {r["fname"] for r in db.query("SELECT fname FROM diary_inbox")}


def file_date(fname):
    """Дата из имени файла (20260823_144606_…). Это дата, когда записку прислали."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})_", fname)
    if not m:
        return date.today()
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return date.today()


def file_author(fname):
    low = fname.lower()
    for key, who in AUTHORS.items():
        if key in low:
            return who
    return "бот"


def draft(fname, text):
    """Записка -> черновик события. Что не распознано — оставляем пустым.

    «Сегодня» в записке — это день, когда её ПРИСЛАЛИ, а не день, когда её разобрал приёмник:
    записка от 22.08 «сегодня пострадал склад» после ночи в ящике означала бы 23-е и уехала бы
    на сутки вперёд. Поэтому точка отсчёта дат — дата из имени файла.
    """
    parsed = biz_diary.parse_free(text, today=file_date(fname))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = lines[0] if lines else text.strip()
    # Заголовок — первая строка, а не весь текст: записка бывает на десять строк, и в ленте
    # от неё нужен смысл одной строкой. Остальное уходит в details целиком, ничего не теряя.
    details = text.strip() if (len(lines) > 1 or len(title) > 200) else None
    return {
        "event_date": parsed["event_date"] or file_date(fname),
        "kind": "mp" if parsed["platform"] else "own",
        "platform": parsed["platform"],
        "account": parsed["account"],
        "title": title[:200],
        "details": details,
        "author": file_author(fname),
        "dedup_key": f"dropbox:{fname}",
    }


def log_taken(fname, event_id, note=None):
    db.query("""INSERT INTO diary_inbox (fname, event_id, note) VALUES (%s, %s, %s)
                ON CONFLICT (fname) DO UPDATE SET event_id = EXCLUDED.event_id,
                                                  note = EXCLUDED.note
                RETURNING fname""",
             (fname, event_id, note))


def run(dry=False, limit=None):
    seen = known()
    fresh = [f for f in notes_on_disk() if f not in seen]
    if limit:
        fresh = fresh[:limit]
    if not fresh:
        print("новых записок нет")
        return
    for fname in fresh:
        try:
            with open(os.path.join(DROPBOX, fname), encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError as exc:
            print(f"  {fname}: не прочитать ({exc})")
            continue
        if not text:
            if not dry:
                log_taken(fname, None, "пустая записка")
            print(f"  {fname}: пусто — пропущено")
            continue
        d = draft(fname, text)
        print(f"  {fname}: {d['event_date']} [{d['kind']}"
              + (f"/{d['platform']}" if d["platform"] else "") + f"] {d['title'][:70]}")
        if dry:
            continue
        new_id = biz_diary.add(mark=MARK, source="dropbox", **d)
        log_taken(fname, new_id, None if new_id else "событие с таким ключом уже было")
        print(f"      → событие #{new_id}" if new_id else "      → уже было, не дублируем")
    print(f"записок: {len(fresh)}" + (" (сухой прогон, ничего не записано)" if dry else ""))


def seed_known(why):
    """Пометить всё, что уже лежит в ящике, как разобранное — чтобы первый боевой прогон
    не завёл задним числом события по запискам, которые сессии перенесли руками."""
    n = 0
    for fname in notes_on_disk():
        db.query("""INSERT INTO diary_inbox (fname, event_id, note) VALUES (%s, NULL, %s)
                    ON CONFLICT (fname) DO NOTHING RETURNING fname""", (fname, why))
        n += 1
    print(f"в журнале отмечено файлов: {n} — «{why}»")


def show():
    rows = db.query("""SELECT fname, taken_at::date::text AS d, event_id, note
                         FROM diary_inbox ORDER BY fname DESC LIMIT 30""")
    for r in rows:
        tail = f"событие #{r['event_id']}" if r["event_id"] else (r["note"] or "без события")
        print(f"  {r['d']}  {r['fname']:55s} {tail}")
    print(f"строк в журнале: {len(rows)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Записки из dropbox-бота -> дневник событий")
    ap.add_argument("--dry", action="store_true", help="показать, ничего не писать")
    ap.add_argument("--limit", type=int, help="взять не больше N записок за прогон")
    ap.add_argument("--list", action="store_true", help="журнал разбора")
    ap.add_argument("--seed-known", action="store_true",
                    help="пометить лежащее в ящике как разобранное (первый запуск)")
    ap.add_argument("--skip", metavar="ФАЙЛ", help="не заводить событие по этой записке")
    ap.add_argument("--why", default="разобрано вручную", help="причина для --skip/--seed-known")
    a = ap.parse_args()
    if a.list:
        show()
    elif a.seed_known:
        seed_known(a.why)
    elif a.skip:
        log_taken(a.skip, None, a.why)
        print(f"{a.skip}: события не заводим — {a.why}")
    else:
        run(dry=a.dry, limit=a.limit)

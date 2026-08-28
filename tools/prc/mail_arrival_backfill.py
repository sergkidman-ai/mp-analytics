# поток: prc — разовый набор истории приходов писем с прайсами в `prc_mail_arrival`.
"""Бэкфилл расписания поставщиков по почте.

Сторож `ops.prc_price_watch` пишет приход сам, но только с того дня, как научился, — а срок
ожидания считать не по чему, пока истории нет. Скрипт добирает её из почты один раз.

Читаем ТОЛЬКО заголовок Date (плюс Subject) — тело письма и вложения не качаем: в папке
«Необработанные товары МС» их больше шестисот, и тянуть их ради даты незачем. Папка
открывается readonly, письма прочитанными не метятся.

В папке несопоставленного поставщик виден только по имени вложения, поэтому там читаем
BODYSTRUCTURE: он приходит СЛЕДУЮЩИМ элементом ответа после кортежа с заголовком.

Запуск: ./venv/bin/python -m tools.prc.mail_arrival_backfill [--since 01-Jul-2026]
"""
import argparse
import email
import re
import sys
from collections import defaultdict
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")

from core.db import upsert
from prices import mailbox, unprocessed
from prices.profiles import PROFILES

MSK = timezone(timedelta(hours=3))
CHUNK = 50                                   # письма тянем пачками: 600 отдельных FETCH — минуты
ATTACH_RE = re.compile(r'"name" "([^"]+)\.txt"', re.I)


def head_date(raw):
    msg = email.message_from_bytes(raw)
    if not msg.get("Date"):
        return None, None
    try:
        return parsedate_to_datetime(msg["Date"]).astimezone(MSK), mailbox._hdr(msg.get("Subject"))
    except (TypeError, ValueError):
        return None, None


def scan(box, folder, since, with_structure=False):
    """Даты писем папки. -> [(dt, subject, structure)]"""
    status, _ = box.select('"%s"' % mailbox.imap_utf7(folder), readonly=True)
    if status != "OK":
        raise RuntimeError(f"IMAP: не открылась папка {folder!r}")
    _, data = box.search(None, f"(SINCE {since})")
    uids = data[0].split()
    items, what = [], ("(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)] BODYSTRUCTURE)"
                       if with_structure else "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)])")
    for i in range(0, len(uids), CHUNK):
        _, resp = box.fetch(b",".join(uids[i:i + CHUNK]).decode(), what)
        pending = None
        for part in resp:
            if isinstance(part, tuple):
                if pending:
                    items.append(pending + ("",))
                dt, subj = head_date(part[1])
                pending = (dt, subj) if dt else None
            elif pending:                     # хвост ответа — здесь и лежит BODYSTRUCTURE
                items.append(pending + (part.decode("utf-8", "replace"),))
                pending = None
        if pending:
            items.append(pending + ("",))
    return items


def first_per_day(rows):
    """Первое письмо каждого дня: расписание считается по нему."""
    best = {}
    for dt, subj, name in rows:
        day = dt.date()
        if day not in best or dt < best[day][0]:
            best[day] = (dt, subj, name)
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description="История приходов прайсов -> prc_mail_arrival")
    ap.add_argument("--since", default="01-Jul-2026", help="дата IMAP-поиска (формат IMAP)")
    args = ap.parse_args(argv)

    box = mailbox.connect()
    found = {}
    try:
        for key, profile in sorted(PROFILES.items()):
            rows = [(dt, subj, "") for dt, subj, _ in scan(box, profile.mail_folder, args.since)]
            found[key] = first_per_day(rows)
        # Папка несопоставленного одна на всех: разносим по имени вложения. Берём ВСЕХ, кого
        # там видно, — Рапид с ВТТ ещё не подключены, но их расписание пусть копится заранее.
        # Поставщиков с настоящим прайсом здесь пропускаем: `colortek.txt` в этой папке — не
        # прайс, а список товара, который внешний загрузчик не сопоставил, и приходит он по
        # своему расписанию. Смешать их значило бы учить сторожа не тому письму, которого он
        # ждёт (на первом прогоне так и вышло: история Колортека схлопнулась с 31 дня до 1).
        un = defaultdict(list)
        for dt, subj, struct in scan(box, unprocessed.FOLDER, args.since, with_structure=True):
            for name in set(ATTACH_RE.findall(struct)):
                if name.lower() not in PROFILES:
                    un[name.lower()].append((dt, subj, name))
        for key, rows in un.items():
            found[key] = first_per_day(rows)
    finally:
        try:
            box.logout()
        except Exception:
            pass

    total = 0
    for key, days in sorted(found.items()):
        rows = [{"supplier_key": key, "letter_date": day, "first_at": dt,
                 "subject": (subj or "")[:300], "filename": name[:200]}
                for day, (dt, subj, name) in sorted(days.items())]
        if rows:
            upsert("prc_mail_arrival", rows, ["supplier_key", "letter_date"])
        total += len(rows)
        span = f"{min(days):%d.%m}..{max(days):%d.%m}" if days else "—"
        print(f"  {key:<14} дней с письмом {len(rows):<4} {span}")
    print(f"всего дней: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

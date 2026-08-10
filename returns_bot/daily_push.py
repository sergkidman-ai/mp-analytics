# поток: ret
"""Ежедневная сводка возвратов в Telegram.

    ./venv/bin/python -m returns_bot.daily_push --to <chat_id>   # разовая отправка одному
    ./venv/bin/python -m returns_bot.daily_push --dry-run        # напечатать текст, не слать
    ./venv/bin/python -m returns_bot.daily_push                  # штатно: собрать и разослать

Перед отправкой обновляет данные (`collect`), чтобы сводка была на сейчас.
"""
import argparse
import sys

from returns_bot import collect as collector
from returns_bot import bot, render, tg


def barcode_photos(org=None):
    """Штрихкод получения возвратов Ozon — только если по аккаунту реально есть что забрать.

    Возвраты Real-FBS сюда не считаются: их выдают на почте по треку, штрихкод Ozon там
    ни при чём и только путает. Коробки вывоза со склада FBO лежат в тех же ПВЗ Ozon —
    их считаем.
    """
    heads, _ = render.fetch(("pickup",), org)
    accounts = sorted({h["account"] for h in heads
                       if h["platform"] == "ozon" and h.get("source") != "ozon_rfbs"})
    out = []
    for account in accounts:
        try:
            out += bot.barcode_files(account)
        except Exception as e:
            print(f"штрихкод Ozon {account}: {type(e).__name__} {str(e)[:100]}")
    return out


def run(targets, dry_run=False, skip_collect=False, with_codes=True):
    if not skip_collect:
        rows, errors = collector.gather()
        if rows:
            collector.store(rows)
        for e in errors:
            print("ОШИБКА сбора", e)

    # по сообщению на юрлицо: за возвратами Цифрового и Дисквэра ездят разные люди
    letters = [(org, text, barcode_photos(org) if with_codes else [])
               for org, text in render.summaries()]

    if dry_run:
        for org, text, photos in letters:
            print(f"===== {org} ===== ({len(text)} знаков, картинок {len(photos)})")
            print(text)
        if not letters:
            print(render.summary())
        print(f"[dry-run] сообщений: {len(letters)}, адресатов: {len(targets)}")
        return 0

    if not targets:
        print("некому слать: пуст TG_RETURNS_NOTIFY_ID / TG_RETURNS_ALLOWED_IDS")
        return 1
    if not letters:                       # забирать нечего — одно короткое сообщение
        letters = [(None, render.summary(), [])]

    ok, failed = 0, []
    for chat_id in targets:
        try:                       # один адресат не нажал Start (403) — остальные всё равно получат
            for org, text, photos in letters:
                # кнопка «Обновить» — под каждой сводкой: обработчик живёт в returns-bot.service
                tg.send(chat_id, text, reply_markup=bot.KB_REFRESH)
                for filename, data, caption, mime in photos:
                    tg.send_document(chat_id, data, filename, caption, mime=mime)
            ok += 1
        except Exception as e:
            failed.append(f"{chat_id}: {type(e).__name__} {str(e)[:120]}")
    for f in failed:
        print("НЕ доставлено", f)
    print(f"отправлено: адресатов {ok} из {len(targets)}, "
          f"сообщений на адресата {len(letters)}, картинок {sum(len(p) for _, _, p in letters)}")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="ежедневная сводка возвратов")
    ap.add_argument("--to", nargs="*", help="chat_id адресатов (по умолчанию из .env)")
    ap.add_argument("--dry-run", action="store_true", help="напечатать текст, ничего не слать")
    ap.add_argument("--no-collect", action="store_true", help="не ходить в API, взять из БД")
    ap.add_argument("--no-codes", action="store_true", help="без картинок штрихкодов")
    a = ap.parse_args(argv)
    targets = a.to or (tg.notify_ids() if not a.dry_run else [])
    return run(targets, a.dry_run, a.no_collect, not a.no_codes)


if __name__ == "__main__":
    sys.exit(main())

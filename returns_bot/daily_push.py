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
from returns_bot import codes, render, tg
from returns_bot.sources import ozon


def barcode_photos():
    """Штрихкод получения возвратов Ozon — только если по аккаунту реально есть что забрать."""
    heads, _ = render.fetch(("pickup",))
    accounts = sorted({h["account"] for h in heads if h["platform"] == "ozon"})
    out = []
    for account in accounts:
        try:
            value, png_b64 = ozon.giveout_barcode(account)
        except Exception as e:
            print(f"штрихкод Ozon {account}: {type(e).__name__} {str(e)[:100]}")
            continue
        png = codes.from_base64(png_b64) or codes.code128(value)
        if png:
            title = ozon.ACCOUNT_TITLE.get(account, account)
            out.append((png, f"🔵 Штрихкод получения возвратов Ozon · {title}"
                             + (f"\n<code>{value}</code>" if value else "")))
    return out


def run(targets, dry_run=False, skip_collect=False, with_codes=True):
    if not skip_collect:
        rows, errors = collector.gather()
        if rows:
            collector.store(rows)
        for e in errors:
            print("ОШИБКА сбора", e)

    text = render.summary()
    photos = barcode_photos() if with_codes else []

    if dry_run:
        print(text)
        print(f"[dry-run] картинок: {len(photos)}, адресатов: {len(targets)}")
        return 0

    if not targets:
        print("некому слать: пуст TG_RETURNS_NOTIFY_ID / TG_RETURNS_ALLOWED_IDS")
        return 1

    ok, failed = 0, []
    for chat_id in targets:
        try:                       # один адресат не нажал Start (403) — остальные всё равно получат
            tg.send(chat_id, text)
            for png, caption in photos:
                tg.send_photo(chat_id, png, caption)
            ok += 1
        except Exception as e:
            failed.append(f"{chat_id}: {type(e).__name__} {str(e)[:120]}")
    for f in failed:
        print("НЕ доставлено", f)
    print(f"отправлено: адресатов {ok} из {len(targets)}, картинок {len(photos)}")
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

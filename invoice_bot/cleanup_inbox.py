#!/usr/bin/env python3
# поток: inv
"""Уборка обработанных вложений invoice-bot: счета/УПД из inbox (Telegram) и
inbox_mail (почта) храним RETAIN_DAYS дней, старше — автоудаление.

Файлы обрабатываются движком синхронно при поступлении, после чего лежат
архивом — «необработанной очереди» в этих папках нет, поэтому удаление по mtime
безопасно (mtime = момент скачивания/обработки).

Запуск (cron, ежедневно):  ./venv/bin/python invoice_bot/cleanup_inbox.py
Предпросмотр без удаления:  ./venv/bin/python invoice_bot/cleanup_inbox.py --dry-run
Срок хранения:              env RETAIN_DAYS (по умолчанию 7).
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DIRS = [os.path.join(BASE, "inbox"), os.path.join(BASE, "inbox_mail")]
LOG = os.path.join(BASE, "cleanup.log")
RETAIN_DAYS = int(os.getenv("RETAIN_DAYS", "7"))


def _log(line):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{stamp}] {line}"
    print(msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def sweep(dry_run=False):
    cutoff = time.time() - RETAIN_DAYS * 86400
    removed, freed, kept = 0, 0, 0
    for d in DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                if dry_run:
                    _log(f"  [dry] удалил бы {os.path.relpath(path, BASE)} "
                         f"({st.st_size} б, mtime {time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))})")
                else:
                    try:
                        os.remove(path)
                    except OSError as e:
                        _log(f"  ОШИБКА удаления {path}: {e}")
                        continue
                removed += 1
                freed += st.st_size
            else:
                kept += 1
    verb = "нашёл к удалению" if dry_run else "удалил"
    _log(f"уборка (хранение {RETAIN_DAYS} дн): {verb} {removed} файлов "
         f"({freed // 1024} КБ), оставил {kept}")
    return removed


if __name__ == "__main__":
    sweep(dry_run="--dry-run" in sys.argv)

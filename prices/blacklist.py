# поток: prc
# -*- coding: utf-8 -*-
"""
Чёрный список артикулов: что уже забраковали в «необработанных» и не хотим видеть снова.

Список ведёт человек и присылает файлом (одна колонка, по артикулу в строке; шапка
«article» необязательна). Загрузка идемпотентна: повторный импорт того же файла ничего
не портит, новые строки добавляются, старые остаются с исходной датой.

    ./venv/bin/python -m prices.blacklist --import /opt/mp-analytics/dropbox/black_list.txt
    ./venv/bin/python -m prices.blacklist --stats
"""
import argparse
import sys
from pathlib import Path

import psycopg2.extras

from core.db import get_conn, query


def norm(article):
    """Ключ сравнения: у поставщиков один код гуляет регистром и пробелами."""
    return str(article or "").strip().upper()


def load_set():
    """Множество нормализованных артикулов ЧС."""
    return {r["article_norm"] for r in query("select article_norm from prc_blacklist")}


def read_file(path):
    """Файл списка -> [артикул]. Пустые строки и шапку выкидываем."""
    out, seen = [], set()
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        article = line.strip().strip('"').strip(";")
        if not article or article.lower() in ("article", "артикул"):
            continue
        key = norm(article)
        if key in seen:
            continue
        seen.add(key)
        out.append(article)
    return out


def add(articles, source=None, note=None):
    """Добавить артикулы в ЧС. Возвращает (сколько было в файле, сколько добавилось нового)."""
    rows = [(norm(a), a, source, note) for a in articles if norm(a)]
    if not rows:
        return 0, 0
    before = len(load_set())
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "insert into prc_blacklist (article_norm, article, source, note) values %s "
                "on conflict (article_norm) do nothing", rows, page_size=500)
    return len(rows), len(load_set()) - before


def mark(skipped, black, reasons=("not_found", "ambiguous")):
    """Переставить причину у строк, чей артикул в ЧС: они больше не новинки.

    Строку не выбрасываем — она остаётся в журнале и в отчёте, но с причиной «blacklisted»,
    поэтому не попадает ни в `prc_unmatched`, ни в файл новинок.
    """
    if not black:
        return skipped, 0
    hits = 0
    for row in skipped:
        if row.get("reason") in reasons and norm(row.get("article")) in black:
            row["reason"] = "blacklisted"
            hits += 1
    return skipped, hits


def main(argv=None):
    ap = argparse.ArgumentParser(description="Чёрный список артикулов")
    ap.add_argument("--import", dest="path", help="файл со списком артикулов")
    ap.add_argument("--note", help="пометка к партии")
    ap.add_argument("--stats", action="store_true", help="сколько в списке сейчас")
    args = ap.parse_args(argv)

    if args.path:
        articles = read_file(args.path)
        total, added = add(articles, source=Path(args.path).name, note=args.note)
        print(f"в файле уникальных артикулов: {total}; добавлено новых: {added}")
    if args.stats or not args.path:
        rows = query("select count(*) n, min(added_at)::date first, max(added_at)::date last "
                     "from prc_blacklist")
        r = rows[0]
        print(f"в чёрном списке: {r['n']} артикулов (с {r['first']} по {r['last']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

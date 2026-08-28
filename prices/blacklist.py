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
import re
import sys
from pathlib import Path

import psycopg2.extras

from core.db import get_conn, query

from . import novelty


# Бренды, которые не берём совсем: правило на префикс артикула, а не перечень кодов —
# у бренда в каждом прайсе новые коды, поимённый список за ними не угонится.
# «ТУ» у Одиссея — товар БЕЗ УПАКОВКИ (решение Сергея 12.08.2026), продавать нечего.
# Набор поставщика: в артикуле через слэш перечислены номера ОДИНОЧНЫХ позиций того же
# прайса («SP364935/364936/364937/364938_MSK» = четыре картриджа Xerox, которые лежат в том
# же файле каждый сам по себе). Остаток у набора и у его составляющих один и тот же товар,
# посчитанный дважды, поэтому наборы Солюшнс принта не заводим и не приходуем — решение
# Сергея 28.08.2026 по итогам разговора с поставщиком. Признак — слэш в артикуле, а НЕ слово
# «комплект» в наименовании: из 5 живых карточек `SP…` со словом «комплект» две — обычный
# одиночный товар («Заправочный комплект SP PC-211EV», арт. SP350131), их правило не трогает.
SP_SET = (r"^(?:SP)?[0-9]+(?:/[0-9]+)+",   # и в нашей форме, и голыми цифрами прайса
          "набор Солюшнс принта — дублирует остаток одиночных позиций (решение Сергея 28.08.2026)")
BRAND_RULES = {
    "odissey": [(r"^ТУ\b", "ТУ — товар без упаковки, не берём (12.08.2026)")],
    # Оба юрлица: МСК и второе, «ООО "Солюшнс принт"» (ключ появится, когда подключим его
    # письмо — правило заводим сразу, чтобы не забыть).
    "s_print_msk": [SP_SET],
    "s_print_spb": [SP_SET],
}


# Виды товара, которые не берём ВООБЩЕ — ни у одного поставщика. Правило именно по ВИДУ из
# названия, а не по артикулу: у чипа артикул — голый код модели картриджа («TK-3110»,
# «ML-D2850B»), а таблица `prc_blacklist` без поставщика, и такой артикул забраковал бы
# настоящий картридж с тем же кодом у любого другого прайса. Название же однозначно.
KIND_RULES = {"chip": "чип — деталь картриджа, чипы не продаём (решение Сергея 28.08.2026)"}


def in_kinds(name):
    """Название попадает под вид, который мы не берём? -> причина или None."""
    return KIND_RULES.get(novelty.kind(str(name or "")))


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


def in_rules(article, supplier_key):
    """Артикул попадает под бракующее правило поставщика? -> причина или None."""
    for pattern, why in BRAND_RULES.get(supplier_key, []):
        if re.match(pattern, str(article or "").strip(), re.IGNORECASE):
            return why
    return None


def mark(skipped, black, reasons=("not_found", "ambiguous"), supplier_key=None):
    """Переставить причину у строк, чей артикул в ЧС: они больше не новинки.

    Строку не выбрасываем — она остаётся в журнале и в отчёте, но с причиной «blacklisted»,
    поэтому не попадает ни в `prc_unmatched`, ни в файл новинок.

    Кроме поимённого списка работают правила по бренду (`BRAND_RULES`): целый бренд
    поставщика, который мы не берём в принципе. Перечнем артикулов такое не закроешь —
    в каждом прайсе у бренда новые коды.
    """
    # Раннего выхода «нечем браковать» здесь нет: правило по виду (`KIND_RULES`) работает
    # всегда и не зависит ни от загруженного списка артикулов, ни от поставщика.
    hits = 0
    for row in skipped:
        if row.get("reason") not in reasons:
            continue
        if (norm(row.get("article")) in black
                or in_rules(row.get("article"), supplier_key)
                or in_kinds(row.get("name"))):
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

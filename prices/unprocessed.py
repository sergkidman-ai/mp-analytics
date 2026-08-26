# поток: prc
# -*- coding: utf-8 -*-
"""
Разбор писем «Необработанные товары МС» — то, что внешний загрузчик не смог сопоставить.

    ./venv/bin/python -m prices.unprocessed --supplier bulat            # проба, в БД не пишет
    ./venv/bin/python -m prices.unprocessed --supplier bulat --apply    # писать в «Новинки»
    ./venv/bin/python -m prices.unprocessed --supplier bulat --file p.txt

Зачем отдельный вход. `prices.run` работает там, где прайс приходит НАМ (профиль в
`prices/profiles.py`). У Булата, ВТТ, Рамис и Блоссома прайса нет вовсе: их грузит внешний
загрузчик по API и присылает письмом только список несопоставленного — по файлу на поставщика,
несколько раз в день, в папку «Прайсы Поставщиков|Необработанные товары МС». В файле от товара
есть один артикул, цена и количество; ни наименования, ни `msId`. Имя добираем у поставщика
(`bulat_site`), дальше — та же дорога, что у всех: ЧС, правила новинок, `catalog.sync`.

В МойСклад отсюда НЕ пишется ничего: строки ложатся во вкладку «Новинки», карточки заводит
человек.
"""
import argparse
import csv
import io
import sys
import datetime as dt
from pathlib import Path

from . import blacklist, bulat_site, catalog, novelty

FOLDER = "Прайсы Поставщиков|Необработанные товары МС"
RAW_DIR = Path(__file__).resolve().parent.parent / "mail_unprocessed"

# supplier_key -> (имя файла во вложении, чем добираем наименование)
SUPPLIERS = {
    "bulat": (r"^bulat\.txt$", bulat_site.resolve),
}


def read_rows(text):
    """`;`-CSV письма -> строки прайса. Пустые sku и служебные хвосты отбрасываем."""
    out = []
    for rec in csv.DictReader(io.StringIO(text), delimiter=";"):
        art = (rec.get("sku") or "").strip()
        if not art:
            continue
        try:
            price = float((rec.get("price") or "0").replace(",", "."))
        except ValueError:
            price = 0.0
        out.append({"article": art, "name": "", "price": price,
                    "qty": (rec.get("quantity") or "").strip(),
                    # Строки письма — по определению ненайденные: у всех пустой msId.
                    # Причина нужна `blacklist.mark` и `novelty.screen`, они смотрят на неё.
                    "reason": "not_found"})
    return out


def source(supplier_key, file_path):
    """(текст файла, откуда взят). Письмо прочитанным НЕ метим: разбор идемпотентен."""
    if file_path:
        return Path(file_path).read_text(encoding="utf-8"), str(file_path)
    from .mailbox import fetch_latest_price
    pattern, _ = SUPPLIERS[supplier_key][0], None
    letter = fetch_latest_price(FOLDER, extensions=(".txt",), pattern=pattern)
    if not letter:
        raise SystemExit(f"в папке {FOLDER!r} нет письма с файлом по ключу {supplier_key}")
    RAW_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    path = RAW_DIR / f"{stamp}_{letter['filename']}"
    path.write_bytes(letter["content"])          # исходник кладём как есть и больше не трогаем
    return letter["content"].decode("utf-8", "replace"), f"{letter['date']} → {path.name}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplier", required=True, choices=sorted(SUPPLIERS))
    ap.add_argument("--file", help="разобрать файл с диска вместо письма")
    ap.add_argument("--apply", action="store_true", help="писать во вкладку «Новинки»")
    ap.add_argument("--no-site", action="store_true",
                    help="имена только из кэша, на сайт поставщика не ходить")
    a = ap.parse_args(argv)

    text, where = source(a.supplier, a.file)
    rows = read_rows(text)
    print(f"источник: {where}")
    print(f"строк в файле: {len(rows)}")
    if not rows:
        return 0

    resolver = SUPPLIERS[a.supplier][1]
    arts = [r["article"] for r in rows]
    if a.no_site:
        names = bulat_site.cached(arts)
    else:
        names = resolver(arts, progress=lambda i, n: print(f"  … имена {i}/{n}"))
    for r in rows:
        r["name"] = names.get(r["article"], "")
    named = [r for r in rows if r["name"]]
    blind = [r for r in rows if not r["name"]]
    print(f"наименование найдено: {len(named)} · не найдено: {len(blind)}")

    # Дальше только те, у кого есть имя: без него ни ЧС по бренду, ни правила новинок,
    # ни шесть признаков не работают — такая строка притворилась бы разобранной.
    named, black_hits = blacklist.mark(named, blacklist.load_set(), supplier_key=a.supplier)
    named, stats, watch = novelty.screen(named, named)
    left = [r for r in named if r["reason"] in ("not_found", "ambiguous")]
    print(f"в ЧС: {black_hits} · отсеяно правилами новинок: {sum(stats.values())}"
          + (" (" + "; ".join(f"{novelty.NOVELTY_REASONS[k][0]} — {v}"
                              for k, v in sorted(stats.items(), key=lambda kv: -kv[1])) + ")"
             if stats else ""))
    print(f"идёт в «Новинки»: {len(left)}")

    if not a.apply:
        print("\n[проба] в БД не писал; повтори с --apply")
        return 0
    matched, auto = catalog.sync(left, a.supplier, None, None)
    print(f"\nзаписано во вкладку «Новинки»: {len(left)}")
    print(f"  закрылось само по артикулу (exists): {auto}")
    print(f"  получило кандидатов на выбор:        {matched - auto}")
    print(f"  без вариантов (настоящая новинка):   {len(left) - matched}")
    if blind:
        print(f"\nбез наименования, в разбор не пошли ({len(blind)}): "
              + ", ".join(r["article"] for r in blind[:6])
              + (" …" if len(blind) > 6 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

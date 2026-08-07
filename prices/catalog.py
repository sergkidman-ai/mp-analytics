# поток: prc
# -*- coding: utf-8 -*-
"""
Сверка новинок поставщика с НАШИМ каталогом по признакам (модель, ресурс, цвет, чип).

Зачем отдельно от загрузки: матчинг прайса в МС строгий — по артикулу, и это правильно
(оприходование не имеет права ошибиться товаром). Но «не нашлось по артикулу» ещё не значит
«у нас такого нет»: тот же C-EXV65 голубой 11000 стр. лежит у нас под кодом 6058 с суффиксом
поставщика, а артикул у каждого поставщика свой. Здесь мы ищем именно ТОВАР, а не строку:
разбираем название на признаки и сравниваем признаки.

Совпадением считаем схождение по всем четырём:
  модель  — общий код в названии/артикуле (C-EXV65, TK-8335, ...);
  цвет    — строго равен;
  ресурс  — расхождение до 25% (поставщики округляют и меряют по-разному);
  чип     — не противоречит (у Кактуса все картриджи с чипом, в наших названиях чип часто
            вообще не упомянут — это «не знаем», а не «без чипа»).

Каталог берём из локальной витрины `ms_product`, в МойСклад не ходим.
"""
import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

from core.db import execute, query

from . import features as F
from .novelty import kind, volume

# Код нашей карточки = число (товар) + суффикс поставщика: 6058sk, 6058sf, 6058gp.
# Товар — это число; суффикс лишь говорит, чьей марки коробка на полке.
CODE_NUM_RE = re.compile(r"^(\d+)")

RESOURCE_TOLERANCE = 0.25
COMMON_CODE_LIMIT = 400      # код, встречающийся чаще, кандидатов не порождает (шум вроде «A4»)
TOP_VARIANTS = 5             # сколько вариантов показывать по одной строке прайса


def load_catalog():
    """Живые карточки каталога с разобранными признаками."""
    rows = query("""
        select ms_id, coalesce(code, '') as code, name, coalesce(article, '') as article
          from ms_product
         where not archived and name is not null
    """)
    out = []
    for row in rows:
        item = dict(row)
        item["num"] = (CODE_NUM_RE.match(item["code"]) or [None])[0]
        item["kind"] = kind(item["name"])
        item.update(F.parse(item["name"], item["article"]))
        out.append(item)
    return out


def build_index(catalog):
    """код -> карточки. Слишком частые коды выкидываем: кандидатов от них миллион, толку ноль."""
    index = defaultdict(list)
    for item in catalog:
        for code in item["codes"]:
            index[code].append(item)
    return {code: items for code, items in index.items() if len(items) <= COMMON_CODE_LIMIT}


def close(want, got):
    """Числа сходятся в пределах допуска (или одного из них нет — тогда не спорим)."""
    if not want or not got:
        return None                                # «не знаем» — не совпадение и не отказ
    return abs(want - got) <= RESOURCE_TOLERANCE * max(want, got)


def measure(item):
    """Чем меряется товар: картридж — ресурсом печати, флакон — объёмом.

    У тонера и чернил ресурса в названии нет и быть не может, зато есть граммы и
    миллилитры, и 100-граммовый флакон — не тот же товар, что 50-граммовый. Признак
    один и тот же по смыслу («сколько внутри»), поэтому и допуск берём общий.
    """
    if item["kind"] in ("toner", "ink"):
        return volume(item["name"])[0]
    return item["resource"]


def chip_ok(want, got):
    """Чип не противоречит. None с любой стороны — молчание, а не «без чипа»."""
    if want is None or got is None:
        return None
    return want == got


def candidates(row, index):
    """Карточки каталога, у которых есть общий код с этой строкой прайса."""
    hits = defaultdict(set)
    for code in row["codes"]:
        for item in index.get(code, ()):
            hits[item["ms_id"]].add(code)
    return hits


def match(row, index, by_id):
    """Варианты каталога для одной строки прайса, лучшие первыми.

    Порядок — по редкости общего кода: совпадение по C-EXV65 весомее совпадения по модели
    принтера, которая стоит у десятка разных расходников (bizhub C250i и Canon iR C250i
    вообще совпали случайно). Подтверждённость признаков — второй ключ: ресурс и чип
    в наших названиях указаны через раз, и их молчание не должно опускать точный код.
    """
    out = []
    for ms_id, shared in candidates(row, index).items():
        item = by_id[ms_id]
        if row["kind"] != item["kind"]:
            continue        # флакон тонера и тонер-картридж на один принтер — разные товары
        if row["color"] and item["color"] and row["color"] != item["color"]:
            continue
        if row["color"] and not item["color"]:
            continue                               # цвет у нас есть, у них нет — не тот товар
        res = close(measure(row), measure(item))
        if res is False:
            continue
        chip = chip_ok(row["chip"], item["chip"])
        if chip is False:
            continue
        rarest = min(len(index[c]) for c in shared)
        best_code = max(shared, key=lambda c: (len(index[c]) == rarest, len(c)))
        confirmed = sum(1 for x in (res, chip) if x)
        out.append({"item": item, "code": best_code, "shared": len(shared),
                    "rarity": rarest, "confirmed": confirmed,
                    "resource_ok": res, "chip_ok": chip})
    out.sort(key=lambda h: (h["rarity"], -h["confirmed"], -h["shared"]))
    return out


def read_novelties(path):
    """Файл новинок внешнего формата: name;price;quantity;msId;defective;Barcode;sku."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        # Режем с КОНЦА: хвост из шести полей фиксирован, а в названии поставщика
        # точка с запятой встречается («…LBP215dw; с чипом»).
        parts = (line.rsplit(";", 6) + [""] * 7)[:7]
        price = parts[1].replace(",", ".").strip()
        rows.append({"name": parts[0], "article": parts[6].strip(),
                     "price": float(price) if price else None})
    return rows


def save(rows, supplier_key, hits_by_article):
    """Разложить новинки и найденные варианты по таблицам.

    Решение человека не трогаем: строка приходит с каждым прогоном прайса, а разбирается
    один раз. Обновляем только описание строки и подсказки — сами варианты пересобираются
    заново, они лишь результат сегодняшней сверки.
    """
    for row in rows:
        found = query("""
            INSERT INTO prc_novelty (supplier_key, article_norm, article, name, kind,
                                     color, measure, chip, price_rub)
            VALUES (%s, upper(btrim(%s)), %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (supplier_key, article_norm) DO UPDATE
               SET name = excluded.name, kind = excluded.kind, color = excluded.color,
                   measure = excluded.measure, chip = excluded.chip,
                   price_rub = excluded.price_rub, last_seen = now()
            RETURNING id
        """, (supplier_key, row["article"], row["article"], row["name"], row["kind"],
              row["color"], measure(row), row["chip"], row["price"]))
        novelty_id = found[0]["id"]
        execute("DELETE FROM prc_novelty_candidate WHERE novelty_id = %s", (novelty_id,))
        for rank, hit in enumerate(hits_by_article.get(row["article"], ()), start=1):
            item = hit["item"]
            execute("""
                INSERT INTO prc_novelty_candidate (novelty_id, rank, ms_id, ms_code, ms_name,
                                                   color, measure, chip, shared_code, verdict)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (novelty_id, rank, item["ms_id"], item["code"], item["name"], item["color"],
                  measure(item), item["chip"], hit["code"], verdict(hit)))


def verdict(hit):
    """Словами: что подтвердилось, а что в каталоге не написано."""
    notes = []
    if hit["resource_ok"] is None:
        notes.append("ресурс/объём не указан")
    if hit["chip_ok"] is None:
        notes.append("чип не указан")
    return "совпало по всем признакам" if not notes else "; ".join(notes)


def analyze(rows, default_chip="chip"):
    """Разобрать строки на признаки и подобрать каждой варианты каталога.

    Возвращает {артикул: [варианты]} — по одному представителю на товар (карточек одного
    товара у нас столько, сколько поставщиков, показывать их все смысла нет).
    """
    catalog = load_catalog()
    by_id = {item["ms_id"]: item for item in catalog}
    index = build_index(catalog)
    hits_by_article = {}
    for row in rows:
        row["kind"] = kind(row["name"])
        row.update(F.parse(row["name"], row["article"]))
        if row["chip"] is None:
            row["chip"] = default_chip
        seen, shown = set(), []
        for hit in match(row, index, by_id):
            key = hit["item"]["num"] or hit["item"]["ms_id"]
            if key in seen:
                continue
            seen.add(key)
            shown.append(hit)
            if len(shown) >= TOP_VARIANTS:
                break
        hits_by_article[row["article"]] = shown
    return hits_by_article


def sync(rows, supplier_key, default_chip=None):
    """Сверить новинки прогона с каталогом и положить во вкладку «Новинки». -> сколько нашлось.

    Строки — {name, article, price}; решение человека по уже разобранным строкам не трогаем.
    """
    if not rows:
        return 0
    hits = analyze(rows, default_chip)
    save(rows, supplier_key, hits)
    return sum(1 for r in rows if hits.get(r["article"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сверка новинок поставщика с каталогом МС")
    ap.add_argument("--file", required=True, help="файл новинок (*_unmatched.txt)")
    ap.add_argument("--supplier-chip", default="chip",
                    help="чип по умолчанию для поставщика (chip|chip_free|nochip|unknown)")
    ap.add_argument("--out", help="куда писать отчёт (CSV)")
    ap.add_argument("--save", action="store_true",
                    help="положить новинки и варианты в БД (вкладка «Новинки» на дашборде)")
    ap.add_argument("--supplier", help="ключ поставщика для БД (по умолчанию — из имени файла)")
    args = ap.parse_args(argv)

    default_chip = None if args.supplier_chip == "unknown" else args.supplier_chip
    rows = read_novelties(args.file)
    hits_by_article = analyze(rows, default_chip)

    out_path = Path(args.out) if args.out else Path(args.file).with_name(
        Path(args.file).name.replace("_unmatched.txt", "_match.csv"))
    found = full = 0
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["артикул", "наименование поставщика", "цвет", "ресурс/объём", "чип",
                         "код МС", "наименование МС", "цвет МС", "ресурс/объём МС", "чип МС",
                         "общий код", "вердикт"])
        for row in rows:
            shown = hits_by_article[row["article"]]
            if shown:
                found += 1
                if shown[0]["confirmed"] == 2:
                    full += 1
            base = [row["article"], row["name"], F.COLOR_NAMES.get(row["color"], ""),
                    measure(row) or "", F.CHIP_NAMES[row["chip"]]]
            if not shown:
                writer.writerow(base + ["", "НЕ НАЙДЕНО В КАТАЛОГЕ", "", "", "", "", ""])
                continue
            for hit in shown:
                item = hit["item"]
                writer.writerow(base + [item["code"], item["name"],
                                        F.COLOR_NAMES.get(item["color"], ""),
                                        measure(item) or "", F.CHIP_NAMES[item["chip"]],
                                        hit["code"], verdict(hit)])
    if args.save:
        supplier = args.supplier or Path(args.file).name.split("_")[0]
        save(rows, supplier, hits_by_article)
        print(f"в БД: prc_novelty, поставщик «{supplier}» — {len(rows)} строк")
    print(f"строк новинок: {len(rows)}")
    print(f"нашлись в каталоге: {found} (из них по всем четырём признакам: {full})")
    print(f"не нашлись: {len(rows) - found}")
    print(f"отчёт: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

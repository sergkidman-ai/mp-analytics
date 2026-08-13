# поток: prc
"""tools/prc/tc_ms_diff.py — где название карточки МС ПРОТИВОРЕЧИТ каталогу TheCartridge.

Каталог ТК — первоисточник: там наш товар заведён один раз, характеристики лежат полями.
Карточки МС — товары поставщиков, заведённые руками с их прайсов, и в их НАЗВАНИЯХ живут
ошибки: «без чипа» у модели, которая в ТК с чипом; ресурс от соседней модели; чужой бренд.
Такая ошибка не косметическая — по названиям МС мы сверяем новинки, и неверный признак либо
прячет верную карточку, либо подсовывает неверную.

Считаем расхождением ТОЛЬКО прямое противоречие: обе стороны высказались и сказали разное.
Молчание МС (в названии признака нет) — не ошибка: названия у поставщиков короткие.

Правок не делает: отчёт CSV → правки в МС руками.

Запуск:  ./venv/bin/python -m tools.prc.tc_ms_diff [--out docs/reports/tc_ms_diff_<дата>.csv]
"""
import argparse
import csv
import datetime
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from prices import catalog as C          # noqa: E402
from prices import features as F         # noqa: E402
from prices.novelty import kind, volume  # noqa: E402
from core.db import query                # noqa: E402

FIELDS = ("внешний код", "код карточки", "наименование МС", "признак",
          "значение МС", "значение ТК", "модель ТК")
NAMES = {"color": "цвет", "resource": "ресурс", "chip": "чип", "brand": "бренд"}


def rows():
    """Строки расхождений. Ресурс сравниваем с тем же допуском, что и матчер (25%)."""
    tc_all = C.load_tc()
    ms = query("""
        select coalesce(code,'') code, name, coalesce(article,'') article,
               coalesce(external_code,'') external_code
          from ms_product
         where not archived and name is not null and external_code is not null
    """)
    out = []
    for row in ms:
        tc = tc_all.get(row["external_code"])
        if not tc:
            continue
        mine = F.parse(row["name"], row["article"])
        item_kind = kind(row["name"])
        # У флакона тонера и чернил ресурса в названии нет — там меряют миллилитрами,
        # а ТК отдаёт страницы. Сравнивать нечего, к расхождениям это не относится.
        bad = []
        if mine["color"] and tc["color"] and mine["color"] != tc["color"]:
            bad.append(("color", mine["color"], tc["color"]))
        if (item_kind not in ("toner", "ink") and mine["resource"] and tc["resource"]
                and C.close(mine["resource"], tc["resource"]) is False):
            bad.append(("resource", mine["resource"], tc["resource"]))
        # Тем же правилом, что и матчер: «с чипом» vs «с чипом без счётчика» — разная
        # подробность, а не ошибка заведения. В отчёт идёт только прямой спор с «без чипа».
        if C.chip_ok(mine["chip"], tc["chip"]) is False:
            bad.append(("chip", F.CHIP_NAMES[mine["chip"]], F.CHIP_NAMES[tc["chip"]]))
        if mine["brand"] and tc["brand"] and not (mine["brand"] & tc["brand"]):
            bad.append(("brand", F.brand_text(mine["brand"]), F.brand_text(tc["brand"])))
        for key, ours, theirs in bad:
            out.append({"внешний код": row["external_code"], "код карточки": row["code"],
                        "наименование МС": row["name"], "признак": NAMES[key],
                        "значение МС": ours, "значение ТК": theirs, "модель ТК": tc["title"]})
    return out


def main():
    day = datetime.date.today().isoformat()
    ap = argparse.ArgumentParser(description="Расхождения названий карточек МС с каталогом ТК")
    ap.add_argument("--out", default=f"docs/reports/tc_ms_diff_{day}.csv")
    args = ap.parse_args()

    data = rows()
    path = BASE_DIR / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(data)

    by_feature, cards = {}, {r["код карточки"] for r in data}
    for r in data:
        by_feature[r["признак"]] = by_feature.get(r["признак"], 0) + 1
    print(f"[tc-ms-diff] расхождений {len(data)} на {len(cards)} карточках")
    for name, n in sorted(by_feature.items(), key=lambda x: -x[1]):
        print(f"[tc-ms-diff]   {name}: {n}")
    print(f"[tc-ms-diff] файл: {args.out}")


if __name__ == "__main__":
    main()

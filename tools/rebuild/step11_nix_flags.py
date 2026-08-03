# поток: gab
"""Шаг 11 — флаги подозрительных коробов в собранном с nix.ru (nix_dims.csv).

Ничего не пересчитывает и не усредняет: размеры остаются ровно теми, что прочитаны
из карточек. Флаг — это ПОМЕТКА, а не повод менять число.

Правило 1 — универсальная коробка бренда: один и тот же набор размеров повторяется
у одного бренда по нескольким РАЗНЫМ моделям. Такие строки в дело не идут.
Правило 2 — выброс вверх: по одной модели несколько источников, и один по объёму
резко больше остальных (порог RATIO). Совпадающие остаются, выброс помечается.

Вход:  docs/rebuild/data/nix_dims.csv (как прислан с домашнего прогона)
Выход: docs/rebuild/data/nix_dims_flagged.csv + сводка в консоль (≤20 строк).
Запуск: python tools/rebuild/step11_nix_flags.py [путь_к_nix_dims.csv]
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import DATA                                    # noqa: E402

csv.field_size_limit(10 ** 7)
MIN_MODELS = 5          # столько разных моделей с одинаковым коробом у одного бренда = универсальная
RATIO = 2.0             # во столько раз больший объём внутри модели = выброс вверх
BRANDS = ['NV-Print', 'NVP', 'Cactus', 'Hi-Black', 'G&G', 'Sakura', 'SolutionPrint', 'Solution Print',
          'ELP', 'T2', '7Q', 'Colortek', 'EasyPrint', 'Easyprint', 'Uniton', 'Netproduct',
          'NetProduct', 'Static Control', 'CET', 'Kyocera', 'Canon', 'HP', 'Xerox', 'Epson',
          'Brother', 'Samsung', 'Ricoh', 'Pantum', 'Lexmark', 'Katun', 'Print-Rite', 'Bion']


def brand(title: str) -> str:
    low = (title or '').lower()
    for b in BRANDS:
        if b.lower() in low:
            return b
    return '(бренд не читается)'


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / 'nix_dims.csv'
    if not src.exists():
        sys.exit(f'Нет файла {src} — сначала нужен прогон сборщика на домашней машине.')
    rows = list(csv.DictReader(src.open(encoding='utf-8-sig'), delimiter=';'))

    for r in rows:
        try:
            d = sorted(float(r[k]) for k in ('Д_мм', 'Ш_мм', 'В_мм'))
        except (ValueError, KeyError):
            r['_box'], r['_vol'] = '', 0.0
            continue
        r['_box'] = 'x'.join(f'{x:g}' for x in d)          # ориентация не важна
        r['_vol'] = d[0] * d[1] * d[2] / 1_000_000          # литры, только для сравнения
        r['бренд'] = brand(r.get('заголовок_карточки', ''))

    have = [r for r in rows if r.get('_box')]

    # ---- правило 1: одна коробка у одного бренда на многих моделях --------------
    models_of = defaultdict(set)
    for r in have:
        models_of[(r['бренд'], r['_box'])].add(r['external_code'])
    universal = {k: v for k, v in models_of.items() if len(v) >= MIN_MODELS}

    # ---- правило 2: выброс вверх внутри модели ----------------------------------
    by_model = defaultdict(list)
    for r in have:
        by_model[r['external_code']].append(r)
    outliers = set()
    for ext, rs in by_model.items():
        vols = sorted({round(x['_vol'], 3) for x in rs}, reverse=True)
        if len(vols) >= 2 and vols[1] > 0 and vols[0] >= RATIO * vols[1]:
            for x in rs:
                if round(x['_vol'], 3) == vols[0]:
                    outliers.add(id(x))

    for r in rows:
        f = []
        key = (r.get('бренд', ''), r.get('_box', ''))
        if key in universal:
            f.append(f'универсальная коробка бренда ({len(universal[key])} моделей)')
        if id(r) in outliers:
            f.append(f'выброс вверх внутри модели (объём ≥{RATIO:g}× следующего)')
        r['флаг'] = '; '.join(f)
        r['объём_л'] = f"{r.get('_vol', 0):.2f}" if r.get('_box') else ''
        r['короб_сорт_мм'] = r.get('_box', '')

    cols = [c for c in rows[0] if not c.startswith('_')] if rows else []
    with (DATA / 'nix_dims_flagged.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)

    print(f'строк {len(rows)} | с размером {len(have)} | моделей с размером {len(by_model)}')
    print(f'правило 1: одинаковых коробов у бренда на ≥{MIN_MODELS} моделях — {len(universal)} наборов, '
          f'строк помечено {sum(1 for r in rows if "универсальная" in r["флаг"])}')
    for (b, box), ms in sorted(universal.items(), key=lambda x: -len(x[1]))[:5]:
        print(f'   {b:<14} {box:<22} моделей {len(ms)}')
    print(f'правило 2: выбросов вверх — {len(outliers)}')
    print('модели с несколькими источниками: '
          f'{sum(1 for v in by_model.values() if len({x["_box"] for x in v}) > 1)}')
    # отдельная проверка двух подозрительных из прошлого прогона — только по найденным источникам
    for ext, name in (('0031', 'Q7516A'), ('0260', '006R01179')):
        rs = by_model.get(ext, [])
        print(f'   {name}: источников {len(rs)} — ' +
              ('; '.join(f'{x["_box"]} мм ({x["объём_л"]} л, {x["бренд"]})' for x in rs[:5])
               or 'в этом прогоне не собрано'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

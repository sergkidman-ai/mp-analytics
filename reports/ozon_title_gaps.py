# поток: mkt
"""reports/ozon_title_gaps.py — список правок заголовков по мид-тейлу Ozon.

Что это. В витрине фраз (`mkt_ozon_query_econ`) есть решение `не_видны`: спрос по фразе
есть, фраза релевантна расходникам, а наша позиция 0 или за 50-й. Ставкой такое чинится
плохо — Ozon просто не считает нас ответом на запрос. Рабочая гипотеза: в заголовке
карточки нет слов запроса. Скрипт эту гипотезу проверяет пословно и отдаёт список правок.

Как читается строка. `missing` — слова фразы, которых НЕТ в заголовке выбранной карточки;
это и есть предлагаемая правка. Если `missing` пуст, слова на месте и дело не в заголовке
(тогда смотреть на характеристики, рич-контент и отзывы).

Карточка на фразу выбирается одна — с лучшей позицией, при равенстве с большей маржой:
править имеет смысл ту, что Ozon и так считает ближайшим кандидатом.

НИЧЕГО НИКУДА НЕ ОТПРАВЛЯЕТ. Только CSV + сводка.

Запуск:  ./venv/bin/python reports/ozon_title_gaps.py [oz_acc1|oz_acc2]
Выход:   docs/reports/ozon_<acc>_title_gaps.csv
"""
import csv
import pathlib
import re
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                          # noqa: E402

OUT_DIR = pathlib.Path('/opt/mp-analytics/docs/reports')
STOP = {'для', 'под', 'как', 'или', 'the', 'and', 'купить', 'цена', 'цены', 'оригинал',
        'на', 'в', 'с', 'и'}

SQL = """
SELECT query, sku, offer_id, name, position, demand, our_price, margin_own_live,
       name_overlap, in_campaign, bid
  FROM mkt_ozon_query_econ
 WHERE account = %(acc)s AND period_start = %(ps)s AND action = 'не_видны'
   AND rel_kind = 'расходник'
 ORDER BY query, (position = 0), position, margin_own_live DESC NULLS LAST
"""


def tokens(s):
    return [t for t in re.sub(r'[^a-zа-я0-9]+', ' ', (s or '').lower()).split()
            if len(t) >= 2 and t not in STOP]


def main(acc='oz_acc1'):
    ps = db.query("""SELECT max(period_start) p FROM mkt_ozon_query_econ
                      WHERE account=%s""", (acc,))[0]['p']
    rows, best = db.query(SQL, {'acc': acc, 'ps': ps}), {}
    for r in rows:                                # первая строка по фразе — лучшая (см. ORDER BY)
        best.setdefault(r['query'], r)

    out = []
    for q, r in best.items():
        miss = [t for t in tokens(q) if t not in (r['name'] or '').lower()]
        out.append([q, int(r['demand'] or 0), int(r['position'] or 0), r['sku'], r['offer_id'],
                    r['name'], ' '.join(miss), len(miss), float(r['our_price'] or 0),
                    float(r['margin_own_live'] or 0), 'да' if r['in_campaign'] else 'нет',
                    float(r['bid'] or 0)])
    out.sort(key=lambda x: -x[1])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f'ozon_{acc}_title_gaps.csv'
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['query', 'demand', 'position', 'sku', 'offer_id', 'name', 'missing',
                    'missing_n', 'price', 'margin_pct', 'in_campaign', 'bid'])
        w.writerows(out)

    fix = [x for x in out if x[7] > 0]
    print(f'{acc} {ps}: фраз «не видны» {len(out)}, из них заголовок чинится {len(fix)}')
    print(f'  спрос под правкой: {sum(x[1] for x in fix)} уникальных искавших за неделю')
    print(f'  карточек затронуто: {len({x[3] for x in fix})}; уже в рекламе {sum(1 for x in fix if x[10] == "да")}')
    print(f'  файл: {path}')
    print('  топ-10 по спросу:')
    for x in fix[:10]:
        print(f"    {x[0][:34]:<34} спрос {x[1]:>4}  поз {x[2]:>3}  нет слов: {x[6][:40]}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'oz_acc1')

#!/usr/bin/env python3
# поток: mkt
"""ops/wb_roy_apply.py — применить недельное решение Роя к ставкам ВБ.

ЗАЧЕМ ОТДЕЛЬНЫЙ ПРИМЕНИТЕЛЬ. Лестница (`wb_bid_ladder`) и понижатель (`wb_bid_lower`) заново
считают свой отбор по своим окнам. Здесь наоборот: применяется РОВНО то, что лежит в отчёте
`docs/reports/mkt_roy_profile_<конец недели>.csv`, который Сергей посмотрел и утвердил.
Иначе между «согласовали список» и «отправили в ВБ» встаёт пересчёт, и уходит не то, что видели.

Решение Сергея 17.08.2026 по неделе 10–16.08:
  🟢 зелёный  → +10 %   (ДРР профиля ниже своего потолка, заказы есть — докладываем)
  🔴 красный  → −10 %   (живой, но дорогой клик; на пол не сбрасываем, потеряем продажи)
  🟤 бордовый → пол     (расход есть, заказов и корзины нет НИГДЕ — ни в рекламе, ни в органике)
  🟡 жёлтый   → не трогаем
  ⚫ вывод / ⬜ маржи нет → НЕ ТРОГАЕМ. Удаление из рекламы отдельным решением, здесь его нет.

Ставка не может выйти за FLOOR..MAX_CPC. Позиция, у которой новая ставка равна старой
(бордовый уже на полу, зелёный уже в потолке), пропускается — пустой PATCH не шлём.

По умолчанию DRY-RUN. Живая запись только с --apply.

Запуск:
  ./venv/bin/python -m ops.wb_roy_apply docs/reports/mkt_roy_profile_2026-08-16.csv
  ./venv/bin/python -m ops.wb_roy_apply docs/reports/mkt_roy_profile_2026-08-16.csv --apply
"""
import csv
import sys
import argparse
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from ops.wb_bid_ladder import FLOOR, MAX_CPC, STEP, apply_step  # noqa: E402

CUT = 0.90
RULES = {'🟢 зелёный': 'up', '🔴 красный': 'down', '🟤 бордовый': 'floor'}


def build(path):
    plan, skip = [], {}
    with open(path, encoding='utf-8-sig') as fh:
        for r in csv.DictReader(fh, delimiter=';'):
            rule = RULES.get(r['цвет'])
            if not rule:
                skip[r['цвет']] = skip.get(r['цвет'], 0) + 1
                continue
            adv = (r['advert_id'] or '').strip()
            if not adv:
                skip['нет advert_id'] = skip.get('нет advert_id', 0) + 1
                continue
            old = float(r['ставка_₽'])
            new = {'up': min(MAX_CPC, old * STEP),
                   'down': max(FLOOR, old * CUT),
                   'floor': FLOOR}[rule]
            new = round(new, 2)
            if abs(new - old) < 0.01:
                skip[f'{r["цвет"]}: ставка уже на месте'] = skip.get(f'{r["цвет"]}: ставка уже на месте', 0) + 1
                continue
            plan.append({'nm_id': int(r['nm_id']), 'advert_id': int(adv), 'color': r['цвет'],
                         'rule': rule, 'old_cpc': old, 'new_cpc': new})
    return plan, skip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('csv_path')
    ap.add_argument('--account', default='wb_acc1')
    ap.add_argument('--apply', action='store_true', help='живая запись в ВБ')
    a = ap.parse_args()

    plan, skip = build(a.csv_path)
    print(f"ПЛАН ПО ОТЧЁТУ {pathlib.Path(a.csv_path).name} · {a.account}")
    print(f"{'цвет':14}{'SKU':>6}{'кампаний':>10}{'ставка была':>13}{'станет':>10}{'дельта ₽':>10}")
    for color in RULES:
        g = [r for r in plan if r['color'] == color]
        if not g:
            continue
        o = sum(r['old_cpc'] for r in g) / len(g)
        n = sum(r['new_cpc'] for r in g) / len(g)
        print(f"{color:14}{len(g):>6}{len({r['advert_id'] for r in g}):>10}"
              f"{o:>13.2f}{n:>10.2f}{n-o:>+10.2f}")
    print(f"{'ИТОГО':14}{len(plan):>6}{len({r['advert_id'] for r in plan}):>10}")
    for k, v in sorted(skip.items(), key=lambda x: -x[1]):
        print(f"  пропуск · {k}: {v}")

    if not a.apply:
        print("\nDRY-RUN. В ВБ не отправлено ничего. Живая запись: добавить --apply")
        return
    note = f"roy weekly {pathlib.Path(a.csv_path).stem}"
    ok, bad = apply_step(a.account, plan, note, author='roy')
    print(f"\nОТПРАВЛЕНО В ВБ: применено {ok}, отклонено {bad}")


if __name__ == '__main__':
    main()

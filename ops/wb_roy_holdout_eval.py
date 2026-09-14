#!/usr/bin/env python3
# поток: mkt
"""ops/wb_roy_holdout_eval.py — разобрать холдаут Роя: работает ли подъём ставки.

ЗАЧЕМ. Весь ретроспективный замер подъёма (когорты 17.08 и 24.08) читался как «подъём убивает
продажи»: заказы у поднятых −62 % при контроле +15 %. Читать так НЕЛЬЗЯ — Рой отбирает 🟢 по
пику прошлой недели, и эта же неделя стоит базой «до», поэтому откат к среднему структурно
гарантирован и неотличим от вреда. Единственное лечение — контрольная группа, выбранная
НЕЗАВИСИМО от результата: `wb_roy_apply --holdout 50`.

ЧТО СЧИТАЕМ. Разность разностей: (после − до) у поднятых МИНУС (после − до) у контроля.
Обе группы отобраны одним и тем же правилом в одну и ту же неделю, поэтому откат к среднему
бьёт по ним ОДИНАКОВО и в разности сокращается. Что не сокращается — эффект ставки.

Метрика — `заказы_всего`, а не рекламные заказы. Подъём ставки умеет перекладывать собственную
органику в платный клик: рекламные заказы растут, общие стоят, деньги потеряны. По рекламному
срезу это выглядит успехом.

ЗАГРЯЗНЕНИЕ. Ставку карточки трогает не только Рой (лестница, понижатель, сторож остатка,
массовая правка с дашборда). Если контрольную карточку за неделю подвинул кто-то ещё, она
больше не контроль. Такие выкидываются, число выброшенных печатается — если их много,
вывод читать нельзя.

Запуск (после того как построился профиль следующей недели):
  ./venv/bin/python -m ops.wb_roy_holdout_eval --end 2026-09-27
  ./venv/bin/python -m ops.wb_roy_holdout_eval --end 2026-09-27 --account wb_acc2
"""
import csv
import sys
import argparse
import datetime
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
REPORTS = BASE_DIR / 'docs' / 'reports'


def read_csv(path):
    with open(path, encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh, delimiter=';'))


def num(v):
    try:
        return float(str(v).replace(',', '.') or 0)
    except ValueError:
        return 0.0


def touched_by_others(account, d1, d2, nm_ids):
    """nm_id, которым за неделю правил ставку кто угодно, кроме недельного прогона Роя."""
    from core import db
    return {r['nm_id'] for r in db.query(
        """select distinct nm_id from wb_bid_log
             where account=%s and applied and nm_id = any(%s)
               and ts::date between %s and %s
               and author not in ('roy', 'roy-rollback')""",
        (account, list(nm_ids), d1, d2))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--end', '--week', dest='end', required=True,
                    help='воскресенье НОВОЙ недели (та, что меряем), YYYY-MM-DD')
    ap.add_argument('--account', default='wb_acc1')
    a = ap.parse_args()

    end = datetime.date.fromisoformat(a.end)
    prev = end - datetime.timedelta(days=7)
    sfx = '' if a.account == 'wb_acc1' else f'_{a.account}'
    hold_path = REPORTS / f'mkt_roy_holdout_{prev}{sfx}.csv'
    prof_path = REPORTS / f'mkt_roy_profile_{end}{sfx}.csv'
    for p in (hold_path, prof_path):
        if not p.exists():
            sys.exit(f'нет файла {p} — замер не строится')

    assign = {int(r['nm_id']): r for r in read_csv(hold_path)}
    after = {int(r['nm_id']): r for r in read_csv(prof_path)}

    dirty = touched_by_others(a.account, prev - datetime.timedelta(days=6), end, assign)
    rows = {'up': [], 'hold': []}
    lost = {'загрязнено чужой правкой': 0, 'нет в профиле новой недели': 0}
    for nm, r in assign.items():
        if nm in dirty:
            lost['загрязнено чужой правкой'] += 1
            continue
        if nm not in after:
            lost['нет в профиле новой недели'] += 1
            continue
        rows[r['группа']].append((num(r['заказы_всего']), num(after[nm]['заказы_всего']),
                                  num(r['расход_₽']), num(after[nm]['расход_₽'])))

    print(f'ХОЛДАУТ {a.account}: неделя назначения {prev}, замер по {end}')
    print(f"{'группа':8}{'SKU':>6}{'заказы до':>12}{'заказы после':>14}{'дельта':>9}"
          f"{'расход до':>12}{'расход после':>14}")
    stat = {}
    for g in ('up', 'hold'):
        v = rows[g]
        if not v:
            print(f'  {g}: пусто')
            continue
        n = len(v)
        o1, o2 = sum(x[0] for x in v) / n, sum(x[1] for x in v) / n
        s1, s2 = sum(x[2] for x in v) / n, sum(x[3] for x in v) / n
        stat[g] = o2 - o1
        print(f'{g:8}{n:>6}{o1:>12.2f}{o2:>14.2f}{o2 - o1:>+9.2f}{s1:>12.0f}{s2:>14.0f}')
    for k, v in lost.items():
        if v:
            print(f'  выброшено · {k}: {v}')

    if len(stat) == 2:
        did = stat['up'] - stat['hold']
        print(f'\nРАЗНОСТЬ РАЗНОСТЕЙ: {did:+.3f} заказа на карточку за неделю.')
        print('Знак — это и есть ответ про подъём: плюс — ставка приносит заказы, '
              'минус — забирает.')
        if min(len(rows['up']), len(rows['hold'])) < 30:
            print('ВНИМАНИЕ: в группе меньше 30 карточек — на одной неделе это шум, '
                  'копить когорты дальше.')


if __name__ == '__main__':
    main()

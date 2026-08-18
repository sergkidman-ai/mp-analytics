#!/usr/bin/env python3
# поток: mkt
"""ops/wb_breadth.py — ШИРИНА показа: сколько карточек перешагнуло порог видимости.

ЗАЧЕМ (вывод 18.08.2026). Ассортимент ВБ работает как лотерея: за неделю 10–16.08 на acc1
продавали 149 карточек, из них 125 неделей раньше не продавали ничего, а 86 прежних отвалились.
82 % недельной выручки дали карточки, которых не было в продажах предыдущей недели. Значит рост
профиля делает НЕ ставка на конкретной карточке, а число карточек, получивших заметный показ:
на acc1 карточек с ≥50 показов в неделю стало 152 → 328, и продающих 110 → 149.

Лестница шансов (acc1, 10–16.08): <10 показов — 0.7 % шанс продажи за неделю, 10–50 — 3.7 %,
50–200 — 13.2 %, 200+ — 33 %. Часть лестницы — самоотбор (ВБ сам даёт показы конвертящим),
поэтому читать её как прогноз нельзя, только как ориентир порога.

Отсюда целевая метрика недели — не ставка и не ДРР по SKU, а «карточек с ≥50 показов».

Печатает лестницу и размер пула (карточки с доказанным спросом, но без показов),
CSV пула кладёт в docs/reports/mkt_wb_breadth_<вс>[_acc].csv.

  ./venv/bin/python -m ops.wb_breadth --end 2026-08-16
  ./venv/bin/python -m ops.wb_breadth --end 2026-08-16 --account wb_acc2
"""
import sys
import csv
import json
import argparse
import datetime
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops import wb_roy_weeks as roy_weeks  # noqa: E402

REP = BASE_DIR / 'docs' / 'reports'
BUCKETS = [(0, 10), (10, 50), (50, 200), (200, 1000), (1000, 10 ** 9)]
THRESHOLD = 50       # порог заметного показа, шанс продажи растёт скачком именно здесь
DEMAND_FROM = '2026-06-01'
MARGIN_MIN = 25.0    # KPI маржи; ниже в рекламу не тащим


def _funnel(account, end, key):
    sfx = '' if account == 'wb_acc1' else f'_{account}'
    p = REP / f'mkt_roy_funnel_{end}{sfx}.json'
    if not p.exists():
        sys.exit(f"нет воронки: {p}")
    return {int(n): v for n, v in json.loads(p.read_text())[key].items()}


def _views(account, d1, d2):
    return {int(r['nm_id']): float(r['v']) for r in db.query(
        """select nm_id, sum(views) v from wb_ad_nm_daily
             where account=%s and dt between %s and %s group by nm_id""", (account, d1, d2))}


def _margin(account):
    d = db.query("select max(captured_date) d from mkt_margin_control where account=%s",
                 (account,))[0]['d']
    if not d:
        return {}, None
    return {int(r['nm_id']): float(r['margin_own_live'] or 0) for r in db.query(
        """select nm_id, margin_own_live from mkt_margin_control
             where account=%s and captured_date=%s""", (account, d))}, d


def run(account, end, w1, w2):
    f2 = _funnel(account, end, 'w2')
    v1, v2 = _views(account, *w1), _views(account, *w2)
    print(f"\n=== {account} · ширина показа · {w2[0]}–{w2[1]} ===")
    wide1 = sum(1 for x in v1.values() if x >= THRESHOLD)
    wide2 = sum(1 for x in v2.values() if x >= THRESHOLD)
    sold2 = sum(1 for r in f2.values() if r['ord'] > 0)
    print(f"карточек с ≥{THRESHOLD} показов: {wide1} → {wide2}   ·   продающих карточек: {sold2}")
    print(f"{'показов/нед':>13}{'карточек':>10}{'продали':>9}{'шанс':>7}{'расход ₽':>10}"
          f"{'выручка ₽':>11}{'ДРР':>7}")
    spend = {int(r['nm_id']): float(r['s']) for r in db.query(
        """select nm_id, sum(spend) s from wb_ad_nm_daily
             where account=%s and dt between %s and %s group by nm_id""", (account, *w2))}
    for lo, hi in BUCKETS:
        g = [n for n, x in v2.items() if lo <= x < hi]
        if not g:
            continue
        sold = [n for n in g if (f2.get(n) or {}).get('ord', 0) > 0]
        sp = sum(spend.get(n, 0) for n in g)
        rev = sum((f2.get(n) or {}).get('sum', 0) for n in g)
        lbl = f"{lo}–{hi}" if hi < 10 ** 9 else f"{lo}+"
        print(f"{lbl:>13}{len(g):>10}{len(sold):>9}{100 * len(sold) / len(g):>6.1f}%"
              f"{sp:>10,.0f}{rev:>11,.0f}{(100 * sp / rev if rev else 0):>6.1f}%")

    demand = {int(r['nm_id']): (int(r['o']), float(r['s'])) for r in db.query(
        """select nm_id, sum(order_count) o, sum(order_sum) s from wb_funnel
             where account=%s and period>=%s group by nm_id having sum(order_count)>0""",
        (account, DEMAND_FROM))}
    marg, md = _margin(account)
    pool = [n for n in demand if v2.get(n, 0) < 10]
    good = [n for n in pool if marg.get(n, 0) >= MARGIN_MIN]
    print(f"пул на расширение: {len(pool)} карточек с продажами с {DEMAND_FROM} получают <10 показов"
          f" ({sum(demand[n][0] for n in pool)} заказов, {sum(demand[n][1] for n in pool):,.0f} ₽)")
    if marg:
        print(f"  из них с маржой ≥{MARGIN_MIN:.0f} %: {len(good)}"
              f" ({sum(demand[n][0] for n in good)} заказов,"
              f" {sum(demand[n][1] for n in good):,.0f} ₽), маржа на {md}")
    else:
        print(f"  МАРЖИ ПО {account} НЕТ (mkt_margin_control пуст) — фильтр по марже не применён")

    sfx = '' if account == 'wb_acc1' else f'_{account}'
    p = REP / f'mkt_wb_breadth_{end}{sfx}.csv'
    with p.open('w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['nm_id', 'заказы_июн_авг', 'выручка_июн_авг_₽', 'показов_за_неделю',
                    'маржа_%', 'проходит_KPI'])
        for n in sorted(pool, key=lambda n: -demand[n][1]):
            m = marg.get(n)
            w.writerow([n, demand[n][0], f"{demand[n][1]:.0f}", f"{v2.get(n, 0):.0f}",
                        f"{m:.1f}" if m is not None else '', 'да' if (m or 0) >= MARGIN_MIN else ''])
    print(f"  список: {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--end', default='')
    ap.add_argument('--account', default='wb_acc1')
    a = ap.parse_args()
    end = datetime.date.fromisoformat(a.end) if a.end else roy_weeks.last_sunday()
    w1, w2 = roy_weeks.weeks(end)
    run(a.account, end, (w1[0].isoformat(), w1[1].isoformat()),
        (w2[0].isoformat(), w2[1].isoformat()))
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
# поток: mkt
"""ops/wb_week_compare.py — неделя против недели по ЛЮБОМУ аккаунту ВБ: профиль и реклама.

ЗАЧЕМ. Проверка Сергея (18.08.2026): второй аккаунт (ДисКвэр) ставок не касался вообще —
в `wb_bid_log` по нему нет ни одной строки. Значит он работает контрольной группой: если
органика там выросла так же, как на «Цифровом квадрате», рост — общий (буст ВБ, сезон, СЕО),
а не наши ставки. Если не выросла — значит подъём всё-таки наш.

Профиль берём из недельной воронки (`ops/wb_roy_weeks`), рекламу — из `wb_ad_nm_daily`.
Недели всегда полные пн-вс, чтобы совпадали дни недели (правило замеров с 13.08.2026).

Запуск:
  ./venv/bin/python -m ops.wb_week_compare --end 2026-08-16
  ./venv/bin/python -m ops.wb_week_compare --end 2026-08-16 --accounts wb_acc1,wb_acc2
"""
import sys
import json
import argparse
import datetime
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops import wb_roy_weeks as roy_weeks  # noqa: E402

REP = BASE_DIR / 'docs' / 'reports'


def _funnel(account, end):
    sfx = '' if account == 'wb_acc1' else f'_{account}'
    p = REP / f'mkt_roy_funnel_{end}{sfx}.json'
    if not p.exists():
        sys.exit(f"нет воронки: {p}\nсначала: ./venv/bin/python -m ops.wb_roy_weeks "
                 f"--account {account} --end {end}")
    d = json.loads(p.read_text())
    return {k: {int(nm): v for nm, v in d[k].items()} for k in ('w1', 'w2')}


def _ads(account, d1, d2):
    r = db.query("""select coalesce(sum(views),0) v, coalesce(sum(clicks),0) c,
                           coalesce(sum(spend),0) s, coalesce(sum(orders),0) o,
                           coalesce(sum(revenue),0) rev, count(distinct nm_id) nm
                      from wb_ad_nm_daily where account=%s and dt between %s and %s""",
                 (account, d1, d2))[0]
    return {k: float(r[k]) for k in ('v', 'c', 's', 'o', 'rev', 'nm')}


def _tot(f):
    return {'open': sum(x['o'] for x in f.values()), 'cart': sum(x['c'] for x in f.values()),
            'ord': sum(x['ord'] for x in f.values()), 'sum': sum(x['sum'] for x in f.values())}


def _d(a, b):
    return '—' if not a else f"{100 * (b - a) / a:+.0f}%"


def report(account, end, w1, w2):
    f = _funnel(account, end)
    t1, t2 = _tot(f['w1']), _tot(f['w2'])
    a1, a2 = _ads(account, *w1), _ads(account, *w2)
    org1, org2 = t1['ord'] - a1['o'], t2['ord'] - a2['o']
    bids = db.query("select count(*) n from wb_bid_log where account=%s and ts between %s and %s",
                    (account, w1[0], f'{end} 23:59'))[0]['n']
    rows = [
        ('открытия карточки', t1['open'], t2['open']),
        ('  из них с рекламы', a1['c'], a2['c']),
        ('  ОРГАНИЧЕСКИЕ открытия', t1['open'] - a1['c'], t2['open'] - a2['c']),
        ('в корзину', t1['cart'], t2['cart']),
        ('ЗАКАЗЫ всего', t1['ord'], t2['ord']),
        ('  из них рекламные', a1['o'], a2['o']),
        ('  органические', org1, org2),
        ('выручка заказов ₽', t1['sum'], t2['sum']),
        ('расход рекламы ₽', a1['s'], a2['s']),
        ('показы рекламы', a1['v'], a2['v']),
        ('клики рекламы', a1['c'], a2['c']),
    ]
    print(f"\n=== {account} · {w1[0]}–{w1[1]} → {w2[0]}–{w2[1]} ===")
    print(f"{'показатель':24}{'было':>12}{'стало':>12}{'дельта':>9}")
    for name, a, b in rows:
        print(f"{name:24}{a:>12,.0f}{b:>12,.0f}{_d(a, b):>9}")
    drr1 = 100 * a1['s'] / t1['sum'] if t1['sum'] else 0
    drr2 = 100 * a2['s'] / t2['sum'] if t2['sum'] else 0
    print(f"{'ДРР профиля':24}{drr1:>11.1f}%{drr2:>11.1f}%")
    cr1 = 100 * (t1['ord'] - a1['o']) / max(1, t1['open'] - a1['c'])
    cr2 = 100 * (t2['ord'] - a2['o']) / max(1, t2['open'] - a2['c'])
    print(f"{'CR органики заказ/открытие':24}{cr1:>11.1f}%{cr2:>11.1f}%")
    top = sorted(((f['w2'].get(nm, {}).get('sum', 0) - v['sum'], nm) for nm, v in f['w1'].items()),
                 reverse=True)[:3]
    extra = [(f['w2'][nm]['sum'], nm) for nm in f['w2'] if nm not in f['w1']]
    top = sorted(top + [(s_, nm) for s_, nm in extra], reverse=True)[:3]
    print("  топ-3 карточки дали " + f"{sum(x[0] for x in top):,.0f} ₽ из "
          f"{t2['sum'] - t1['sum']:+,.0f} ₽ прироста выручки: "
          + ', '.join(str(nm) for _, nm in top))
    print(f"правок ставок за окно: {bids}"
          + ("  ← ставок не касались, это контрольная группа" if not bids else ""))
    return {'org1': org1, 'org2': org2, 'open1': t1['open'], 'open2': t2['open'],
            'oo1': t1['open'] - a1['c'], 'oo2': t2['open'] - a2['c'],
            'sum1': t1['sum'], 'sum2': t2['sum'], 'bids': bids, 'acc': account, 'cr1': cr1, 'cr2': cr2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--end', default='')
    ap.add_argument('--accounts', default='wb_acc1,wb_acc2')
    a = ap.parse_args()
    end = datetime.date.fromisoformat(a.end) if a.end else roy_weeks.last_sunday()
    w1, w2 = roy_weeks.weeks(end)
    w1 = (w1[0].isoformat(), w1[1].isoformat())
    w2 = (w2[0].isoformat(), w2[1].isoformat())

    res = [report(acc.strip(), end, w1, w2) for acc in a.accounts.split(',') if acc.strip()]
    if len(res) == 2:
        print("\n=== ВЫВОД ===")
        for r in res:
            print(f"  {r['acc']}: органические открытия {_d(r['oo1'], r['oo2'])}, "
                  f"CR органики {r['cr1']:.1f}%→{r['cr2']:.1f}%, "
                  f"органические заказы {_d(r['org1'], r['org2'])}, правок ставок {r['bids']}")
        ctrl = next((r for r in res if not r['bids']), None)
        live = next((r for r in res if r['bids']), None)
        if ctrl and live:
            tr = 100 * (ctrl['oo2'] - ctrl['oo1']) / max(1, ctrl['oo1'])
            trl = 100 * (live['oo2'] - live['oo1']) / max(1, live['oo1'])
            print(f"  Органический ТРАФИК не вырос ни там, ни там ({trl:+.0f}% / {tr:+.0f}%) — "
                  f"буста выдачи от ВБ на этой паре недель не видно."
                  if max(tr, trl) < 10 else
                  f"  Органический трафик: {trl:+.0f}% против {tr:+.0f}% на контроле.")
            dc, dl = ctrl['cr2'] - ctrl['cr1'], live['cr2'] - live['cr1']
            if dc >= dl * 0.6:
                print(f"  КОНВЕРСИЯ органики выросла одинаково и там, где ставок не касались "
                      f"({dl:+.1f} п.п. против {dc:+.1f} п.п.) → это спрос/сезон, не ставки.")
            else:
                print(f"  Конверсия органики на контроле выросла слабее "
                      f"({dc:+.1f} п.п. против {dl:+.1f} п.п.) → часть эффекта своя.")
    return 0


if __name__ == '__main__':
    sys.exit(main())

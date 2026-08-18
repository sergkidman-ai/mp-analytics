# поток: mkt
# ozon_acc2_cleanup.py — чистка рекламы acc2 по паспорту SKU:
#   E1 — снять из кампаний (клики есть, заказов нет, товар не продавался ни разу);
#   E2 — понизить ставку (рекламных заказов нет, но товар продаётся сам) половине когорты,
#        вторая половина — случайный контроль, чтобы через 2 недели увидеть цену снижения.
#
#   run           — посчитать и показать план (в кабинет ничего не уходит)
#   run --apply   — отправить (только по прямой команде Сергея)
#
# Перед снятием список ВСЕГДА пишется в docs/reports/ozon_acc2_e1_removed.csv:
# на волне 1 (08.08) список не сохранили, и восстановить его теперь можно только по снимкам.
import sys, csv, json, time, random, argparse, datetime as dt
sys.path.insert(0, '/opt/mp-analytics')
import requests
from core import db
from collectors.ozon_ads import _token, PERF
from tools.ozon_bid_ramp import _apply_bids
from tools.ozon_weekly_bids import ensure_journal

SEED = 20260818
E2_CUT = 0.30       # шаг снижения ставки: -10 % на нулевой конверсии читается месяцами
BATCH = 100
SCAN = '/opt/mp-analytics/docs/reports/ozon_{}_sku_scan.csv'
OUT = '/opt/mp-analytics/docs/reports/ozon_{}_e1_removed.csv'


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def load(acc):
    with open(SCAN.format(acc.replace('oz_', ''))) as fh:
        fh.readline()
        return list(csv.DictReader(fh))


def pairs(acc):
    """связки кампания × SKU из последнего снимка состава"""
    out = {}
    for r in db.query("""SELECT campaign_id::text campaign_id, sku::text sku, bid::float bid,
              campaign_title FROM ozon_bids WHERE account=%s
              AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)""", (acc, acc)):
        out.setdefault(r['sku'], []).append(r)
    return out


def _delete(acc, cid, skus):
    H = {'Authorization': f'Bearer {_token(acc)}', 'Content-Type': 'application/json'}
    r = requests.post(f'{PERF}/api/client/campaign/{cid}/products/delete',
                      headers=H, json={'sku': [str(s) for s in skus]}, timeout=180)
    return r.status_code, r.text[:200]


def _journal(acc, week, today, p, action, applied, resp, bid_after=None, reason=''):
    db.execute("""INSERT INTO mkt_ozon_bid_journal
          (decided_on,week_start,account,campaign_id,sku,offer_id,name,tier,action,bid_before,bid_after,
           reason,m_before,applied,applied_at,api_response,review_on)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (week_start,account,campaign_id,sku) DO UPDATE SET
          decided_on=EXCLUDED.decided_on, action=EXCLUDED.action, bid_before=EXCLUDED.bid_before,
          bid_after=EXCLUDED.bid_after, reason=EXCLUDED.reason, m_before=EXCLUDED.m_before,
          applied=EXCLUDED.applied, applied_at=EXCLUDED.applied_at,
          api_response=EXCLUDED.api_response, review_on=EXCLUDED.review_on""",
        (today, week, acc, p['cid'], p['sku'], p['offer_id'], p['name'][:200], p['tier'], action,
         p['bid'] or None, bid_after, reason,
         json.dumps({'ad_spend_win': p['spend'], 'ad_clicks': p['clicks'], 'rev90': p['rev90'],
                     'qty_all': p['qty_all']}),
         applied, dt.datetime.now() if applied else None, resp, today + dt.timedelta(days=14)))


def run(acc, apply_, week=None):
    ensure_journal()
    today = dt.date.today()
    week = week or str(today - dt.timedelta(days=today.weekday()))
    rows = {r['sku']: r for r in load(acc)}
    comp = pairs(acc)
    e1, e2 = [], []
    for sku, r in rows.items():
        if r['tier'] not in ('E1', 'E2'):
            continue
        for c in comp.get(sku, []):
            p = {'sku': sku, 'cid': c['campaign_id'], 'title': c['campaign_title'],
                 'bid': f(c['bid']), 'offer_id': r['offer_id'], 'name': r['name'], 'tier': r['tier'],
                 'spend': f(r['ad_spend']), 'clicks': f(r['ad_clicks']), 'rev90': f(r['rev90']),
                 'qty_all': f(r['qty_all'])}
            (e1 if r['tier'] == 'E1' else e2).append(p)
    # E2 режем пополам со случайным контролем — правило 17.08
    rnd = random.Random(SEED)
    skus2 = sorted({p['sku'] for p in e2})
    rnd.shuffle(skus2)
    down = set(skus2[:len(skus2) // 2])
    e2_down = [p for p in e2 if p['sku'] in down]
    e2_ctrl = [p for p in e2 if p['sku'] not in down]
    print(f'{acc}: E1 снять — {len(e1)} связок ({len({p["sku"] for p in e1})} SKU), '
          f'расход {sum(p["spend"] for p in {p["sku"]: p for p in e1}.values()):,.0f} ₽ за окно'.replace(',', ' '))
    print(f'{acc}: E2 понизить −{E2_CUT:.0%} — {len(e2_down)} связок ({len(down)} SKU), '
          f'контроль {len(e2_ctrl)} связок ({len(skus2) - len(down)} SKU), seed {SEED}')
    print(f'   сумма ставок E2: {sum(p["bid"] for p in e2_down):,.0f} → '
          f'{sum(round(p["bid"] * (1 - E2_CUT), 2) for p in e2_down):,.0f} ₽'.replace(',', ' '))
    if not apply_:
        print('   [DRY-RUN, на площадку ничего не уходит]')
        return
    # список снятых сохраняем ДО отправки
    with open(OUT.format(acc.replace('oz_', '')), 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['snapshot_date', 'campaign_id', 'campaign_title', 'sku', 'offer_id', 'name',
                    'bid', 'ad_spend_window', 'ad_clicks', 'rev90'])
        for p in e1:
            w.writerow([today, p['cid'], p['title'], p['sku'], p['offer_id'], p['name'],
                        p['bid'], p['spend'], p['clicks'], p['rev90']])
    by_camp = {}
    for p in e1:
        by_camp.setdefault(p['cid'], []).append(p)
    ok = bad = 0
    for cid, ps in by_camp.items():
        for i in range(0, len(ps), BATCH):
            chunk = ps[i:i + BATCH]
            code, resp = _delete(acc, cid, [p['sku'] for p in chunk])
            good = code in (200, 201)
            ok += len(chunk) if good else 0
            bad += 0 if good else len(chunk)
            for p in chunk:
                _journal(acc, week, today, p, 'remove', good, f'{code} {resp[:120]}',
                         reason=f"E1: {int(p['clicks'])} кликов, 0 заказов, продаж не было вовсе; "
                                f"расход {p['spend']:.0f} ₽ за окно")
            time.sleep(1.0)
    print(f'E1: снято {ok}, ошибок {bad} → список в {OUT.format(acc.replace("oz_", ""))}')
    ok = bad = 0
    by_camp = {}
    for p in e2_down:
        by_camp.setdefault(p['cid'], []).append(p)
    for cid, ps in by_camp.items():
        for i in range(0, len(ps), BATCH):
            chunk = ps[i:i + BATCH]
            items = [(p['sku'], round(p['bid'] * (1 - E2_CUT), 2)) for p in chunk]
            code, resp = _apply_bids(acc, cid, items)
            if code not in (200, 201) and len(chunk) > 1:      # пол аукциона у отдельных SKU
                for p, it in zip(chunk, items):
                    c2, r2 = _apply_bids(acc, cid, [it])
                    g2 = c2 in (200, 201)
                    ok += g2; bad += (not g2)
                    _journal(acc, week, today, p, 'bid_down', g2, f'{c2} {r2[:120]}', it[1],
                             f"E2 −{E2_CUT:.0%}: реклама заказов не даёт, товар продаётся сам "
                             f"({p['rev90']:,.0f} ₽/90 дн)".replace(',', ' '))
                    time.sleep(0.3)
                continue
            good = code in (200, 201)
            ok += len(chunk) if good else 0
            bad += 0 if good else len(chunk)
            for p, it in zip(chunk, items):
                _journal(acc, week, today, p, 'bid_down', good, f'{code} {resp[:120]}', it[1],
                         f"E2 −{E2_CUT:.0%}: реклама заказов не даёт, товар продаётся сам "
                         f"({p['rev90']:,.0f} ₽/90 дн)".replace(',', ' '))
            time.sleep(1.0)
    for p in e2_ctrl:
        _journal(acc, week, today, p, 'e2_control', False, 'контроль: ставку не трогаем', p['bid'],
                 'E2 контрольная половина — сравнение через 2 недели по позиции, показам и штукам')
    print(f'E2: понижено {ok}, ошибок {bad}; контроль {len(e2_ctrl)} связок записан без изменений')
    print(f'журнал: mkt_ozon_bid_journal, week_start={week}, сверка {today + dt.timedelta(days=14)}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['run'])
    ap.add_argument('--account', default='oz_acc2')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--week', default=None)
    a = ap.parse_args()
    run(a.account, a.apply, a.week)

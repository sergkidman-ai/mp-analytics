# поток: mkt
# ozon_core_push.py — шаг «ДРР 3 %»: вся прибавка бюджета идёт ТОЛЬКО на ядро выручки.
#
#   plan          — посчитать ядро, разбить пополам (половина — контроль), показать план
#   plan --apply  — отправить ставки в Ozon и записать решения в mkt_ozon_bid_journal
#
# Логика (план 17.08, артефакт «Возврат acc1 к росту», шаг 2):
#   * ядро = SKU, дающие CORE_SHARE выручки за 90 дней (по паспорту ozon_<acc>_sku_scan.csv);
#   * ядро делится пополам ПАРАМИ по рангу выручки: один из пары в подъём, второй — контроль,
#     иначе через 3 недели нельзя отличить эффект ставки от сезона;
#   * потолок CPC — margin_safe и конверсия: фактическая, если накоплено >= CR_MIN_CLICKS кликов
#     и >= CR_MIN_ORDERS заказов, иначе справочная по ценовой корзине; коридор x0.3..x3 (правило 17.08);
#   * ставка только вверх и не более чем в MAX_FACTOR раз за шаг;
#   * если прогноз расхода выше недельного потолка, подъёмы ужимаются пропорционально;
#   * ядро без рекламы заводится в кампанию своей ценовой корзины.
import sys, csv, json, time, random, argparse, datetime as dt
sys.path.insert(0, '/opt/mp-analytics')
import requests
from core import db
from collectors.ozon_ads import _token, PERF
from tools.ozon_bid_ramp import _apply_bids
from tools.ozon_sku_scan import PPO_RATE
from tools.ozon_weekly_bids import ensure_journal

SEED = 20260818
CORE_SHARE = 0.50          # доля выручки за 90 дней, которую считаем ядром
WEEK_SPEND_TARGET = 63000  # ДРР 3 % от ~2,1 млн ₽/нед
MAX_FACTOR = 3.0           # больше чем втрое за шаг ставку не поднимаем
CR_MIN_CLICKS, CR_MIN_ORDERS = 30, 3
CR_CORRIDOR = (0.3, 3.0)
SCAN = '/opt/mp-analytics/docs/reports/ozon_{}_sku_scan.csv'
BUCKET_CAMPAIGN = {  # ценовая корзина -> кампания acc1 (для ядра без рекламы)
    'от 0 до 1000': (0, 1000), 'от 1000-2000': (1000, 2000), 'от 2000 до 5000': (2000, 5000),
    'от 5000 до 10000': (5000, 10000), 'от 10000 до 500 тыс': (10000, 10 ** 9)}


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def read_scan(acc):
    path = SCAN.format(acc.replace('oz_', ''))
    with open(path) as fh:
        head = fh.readline()
        rows = list(csv.DictReader(fh))
    return path, head.strip(), rows


def cr_eff(r):
    """Конверсия для потолка: фактическая при накопленной статистике, иначе справочная."""
    ref = f(r['cr_bucket'])
    if f(r['ad_clicks']) >= CR_MIN_CLICKS and f(r['ad_orders']) >= CR_MIN_ORDERS:
        act = f(r['ad_orders']) / f(r['ad_clicks'])
        return max(ref * CR_CORRIDOR[0], min(act, ref * CR_CORRIDOR[1])), 'факт'
    return ref, 'справ'


def ceiling(r):
    pr, mg = f(r['our_price']), r['margin_safe']
    if not pr or mg in ('', None):
        return None, None
    cr, src = cr_eff(r)
    return round((pr * f(mg) / 100 - pr * PPO_RATE) * cr, 2), src


def live_bids(acc, week):
    """Фактическая ставка: снимок ozon_bids снимается утром, поэтому вчерашний откат в нём
    ещё не виден — последнее применённое решение журнала важнее снимка."""
    return {(r['campaign_id'], r['sku']): float(r['bid_after']) for r in db.query(
        """SELECT DISTINCT ON (campaign_id, sku) campaign_id::text campaign_id, sku::text sku, bid_after
             FROM mkt_ozon_bid_journal WHERE account=%s AND week_start=%s AND applied AND bid_after IS NOT NULL
            ORDER BY campaign_id, sku, applied_at DESC""", (acc, week))}


def campaigns(acc):
    rows = db.query("""SELECT campaign_id::text c, min(campaign_title) t, count(*) n,
        min(bid)::float mn, percentile_disc(0.5) WITHIN GROUP (ORDER BY bid)::float med
        FROM ozon_bids WHERE account=%s AND captured_at=(SELECT max(captured_at) FROM ozon_bids
        WHERE account=%s) GROUP BY 1""", (acc, acc))
    return {r['t']: r for r in rows}


def sku_campaign(acc):
    return {str(r['sku']): (str(r['campaign_id']), float(r['bid'])) for r in db.query(
        """SELECT DISTINCT ON (sku) sku, campaign_id, bid FROM ozon_bids WHERE account=%s
           AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)
           ORDER BY sku, bid DESC""", (acc, acc))}


def build(acc):
    path, head, rows = read_scan(acc)
    for r in rows:
        r['_rev90'] = f(r['rev90'])
    rows.sort(key=lambda r: -r['_rev90'])
    total = sum(r['_rev90'] for r in rows)
    core, acc_rev = [], 0.0
    for r in rows:
        if acc_rev >= CORE_SHARE * total:
            break
        core.append(r)
        acc_rev += r['_rev90']
    # деление пополам парами по рангу выручки: сезон и величина товара распределены поровну
    rnd = random.Random(SEED)
    push, ctrl = [], []
    for i in range(0, len(core), 2):
        pair = core[i:i + 2]
        if len(pair) == 1:
            (push if rnd.random() < 0.5 else ctrl).append(pair[0])
            continue
        a, b = (pair if rnd.random() < 0.5 else pair[::-1])
        push.append(a); ctrl.append(b)
    return path, head, rows, total, core, push, ctrl


def cmd_plan(acc, apply_, week=None):
    ensure_journal()
    today = dt.date.today()
    week = week or str(today - dt.timedelta(days=today.weekday()))
    path, head, rows, total, core, push, ctrl = build(acc)
    camps, sku_cmp, live = campaigns(acc), sku_campaign(acc), live_bids(acc, week)
    ads_days = 22
    cur_week_spend = sum(f(r['ad_spend']) for r in rows) / ads_days * 7
    plan_up, plan_new, skipped = [], [], []
    for r in push:
        ceil_, src = ceiling(r)
        in_ads = r['in_ads'] == 'True'
        if ceil_ is None:
            skipped.append((r, 'нет маржи/цены')); continue
        if in_ads:
            cid0 = sku_cmp.get(r['sku'], (None, None))[0]
            bid = live.get((cid0, r['sku']), f(r['bid']))
            if f(r['ad_orders']) < 1:
                skipped.append((r, 'реклама не даёт заказов — ставку не поднимаем')); continue
            tgt = min(ceil_, bid * MAX_FACTOR)
            if tgt <= bid + 0.5:
                skipped.append((r, f'запаса нет: ставка {bid:.0f} ₽, потолок {ceil_:.0f} ₽')); continue
            cid = cid0
            spend_w = f(r['ad_spend']) / ads_days * 7
            plan_up.append({'r': r, 'cid': cid, 'bid': bid, 'tgt': round(tgt, 2), 'ceil': ceil_,
                            'src': src, 'spend_w': spend_w, 'proj': spend_w * tgt / bid})
        else:
            pr = f(r['our_price'])
            title = next((t for t, (lo, hi) in BUCKET_CAMPAIGN.items() if lo <= pr < hi), None)
            c = camps.get(title)
            if not c:
                skipped.append((r, f'нет кампании под цену {pr:.0f} ₽')); continue
            tgt = max(min(ceil_, c['med'] * 1.2), c['mn'])
            plan_new.append({'r': r, 'cid': c['c'], 'title': title, 'bid': 0.0,
                             'tgt': round(tgt, 2), 'ceil': ceil_, 'src': src, 'spend_w': 0.0,
                             'proj': tgt * 4})  # ~4 клика/нед на старте — грубая оценка
    proj = cur_week_spend + sum(p['proj'] - p['spend_w'] for p in plan_up + plan_new)
    scale = 1.0
    if proj > WEEK_SPEND_TARGET:
        head_room = WEEK_SPEND_TARGET - cur_week_spend
        add = sum(p['proj'] - p['spend_w'] for p in plan_up + plan_new)
        scale = max(0.0, head_room / add) if add else 0.0
        for p in plan_up + plan_new:
            p['tgt'] = round(p['bid'] + (p['tgt'] - p['bid']) * scale, 2) if p['bid'] else \
                round(p['tgt'] * max(scale, 0.5), 2)
    print(head)
    print(f'\nядро: {len(core)} SKU дают {CORE_SHARE:.0%} выручки 90 дн ({sum(r["_rev90"] for r in core):,.0f} '
          f'из {total:,.0f} ₽); подъём {len(push)}, контроль {len(ctrl)} (seed {SEED})'.replace(',', ' '))
    print(f'сейчас расход {cur_week_spend:,.0f} ₽/нед, цель ДРР 3 % = {WEEK_SPEND_TARGET:,.0f} ₽/нед'
          .replace(',', ' '))
    print(f'подъём ставки: {len(plan_up)} SKU в рекламе, завести новых: {len(plan_new)}, '
          f'без запаса/данных: {len(skipped)}')
    if plan_up:
        print(f'  ставки {sum(p["bid"] for p in plan_up):,.0f} → {sum(p["tgt"] for p in plan_up):,.0f} ₽'
              .replace(',', ' ') + (f' (ужато до {scale:.0%} — упёрлись в потолок бюджета)' if scale < 1 else ''))
    print(f'прогноз расхода после шага: {min(proj, WEEK_SPEND_TARGET):,.0f} ₽/нед'.replace(',', ' ')
          + ('' if apply_ else '   [DRY-RUN, на площадку ничего не уходит]'))
    if not apply_:
        for p in (plan_up + plan_new)[:8]:
            r = p['r']
            print(f"   {r['sku']:>11} {r['offer_id']:<10} {r['name'][:34]:<34} "
                  f"{p['bid']:>5.1f} → {p['tgt']:>5.1f} ₽ (потолок {p['ceil']:.0f}, CR {p['src']}, "
                  f"продажи 90 дн {r['_rev90']:,.0f} ₽)".replace(',', ' '))
        return
    ok_n = bad_n = 0
    for p in plan_up + plan_new:
        r = p['r']
        new = p['tgt'] in plan_new
        code, resp = _apply_bids(acc, p['cid'], [(r['sku'], p['tgt'])]) if p in plan_up else \
            _add_products(acc, p['cid'], [(r['sku'], p['tgt'])])
        good = code in (200, 201)
        ok_n += good; bad_n += (not good)
        _journal(acc, week, today, p, 'core_push', good, f'{code} {resp[:120]}')
        time.sleep(0.4)
    for r in ctrl:
        _journal(acc, week, today, {'r': r, 'cid': sku_cmp.get(r['sku'], ('0', 0))[0] or '0',
                                    'bid': f(r['bid']), 'tgt': f(r['bid']), 'ceil': ceiling(r)[0],
                                    'src': ceiling(r)[1], 'spend_w': 0, 'proj': 0},
                 'core_control', False, 'контроль: ставку не трогаем')
    print(f'итог: применено {ok_n}, ошибок {bad_n}; контроль {len(ctrl)} SKU записан без изменений '
          f'(mkt_ozon_bid_journal, week_start={week})')


def _add_products(acc, cid, items):
    """Завести товар в кампанию: POST /api/client/campaign/{id}/products (см. ozon_new_campaigns.py)."""
    H = {'Authorization': f'Bearer {_token(acc)}', 'Content-Type': 'application/json'}
    body = {'bids': [{'sku': str(s), 'bid': str(int(round(b * 1_000_000)))} for s, b in items]}
    r = requests.post(f'{PERF}/api/client/campaign/{cid}/products', headers=H, json=body, timeout=120)
    return r.status_code, r.text[:300]


def _journal(acc, week, today, p, action, applied, resp):
    r = p['r']
    reason = (f"ядро выручки ({r['_rev90']:,.0f} ₽/90 дн): ".replace(',', ' ') +
              (f"ставка {p['bid']:.1f} → {p['tgt']:.1f} ₽ при потолке {p['ceil']:.0f} ₽ (CR {p['src']})"
               if action == 'core_push' else
               f"контрольная половина — ставку не трогаем до сверки через 3 недели"))
    db.execute("""INSERT INTO mkt_ozon_bid_journal
          (decided_on,week_start,account,campaign_id,sku,offer_id,name,tier,action,bid_before,bid_after,
           reason,m_before,applied,applied_at,api_response,review_on)
          VALUES (%s,%s,%s,%s,%s,%s,%s,'core',%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (week_start,account,campaign_id,sku) DO UPDATE SET
          decided_on=EXCLUDED.decided_on, action=EXCLUDED.action, bid_before=EXCLUDED.bid_before,
          bid_after=EXCLUDED.bid_after, reason=EXCLUDED.reason, m_before=EXCLUDED.m_before,
          applied=EXCLUDED.applied, applied_at=EXCLUDED.applied_at,
          api_response=EXCLUDED.api_response, review_on=EXCLUDED.review_on""",
        (today, week, acc, p['cid'], r['sku'], r['offer_id'], r['name'][:200], action,
         p['bid'] or None, p['tgt'] or None, reason,
         json.dumps({'rev90': r['_rev90'], 'ad_spend_win': f(r['ad_spend']),
                     'ad_orders_win': f(r['ad_orders']), 'ceiling': p['ceil'], 'cr_src': p['src']}),
         applied, dt.datetime.now() if applied else None, resp, today + dt.timedelta(days=21)))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['plan'])
    ap.add_argument('--account', default='oz_acc1')
    ap.add_argument('--apply', action='store_true', help='ОТПРАВИТЬ в Ozon (только по команде Сергея)')
    ap.add_argument('--week', default=None)
    a = ap.parse_args()
    cmd_plan(a.account, a.apply, a.week)

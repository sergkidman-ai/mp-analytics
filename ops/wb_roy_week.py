#!/usr/bin/env python3
# поток: mkt
"""Недельный отчёт Роя: раскладка всех ставочных SKU по четырём цветам + список на вывод.

Окно 10–16.08.2026 — полная неделя пн-вс, и реклама, и Джем совпадают день в день
(period_start=10.08 у Джема = скользящее окно 10–16.08). Сравнивать нечего с чем — окна одни.
"""
import csv
import io
import sys

sys.path.insert(0, '/opt/mp-analytics')
from core import db
from reports.bid_policy import WB_MARGIN_GATE, WB_MARGIN_FLOOR

ACC, FLOOR, D1, D2, JAM = 'wb_acc1', 7.30, '2026-08-10', '2026-08-16', '2026-08-10'

ads = {r['nm_id']: r for r in db.query("""
    select nm_id, sum(views) v, sum(clicks) c, sum(orders) o,
           sum(spend) s, sum(revenue) rev
      from wb_ad_nm_daily where account=%s and dt between %s and %s group by 1""",
                                       (ACC, D1, D2))}
org = {r['nm_id']: r for r in db.query("""
    select nm_id, open_card, add_to_cart, orders, avg_position, visibility
      from wb_search_report where account=%s and period_start=%s""", (ACC, JAM))}
bids = {r['nm_id']: (float(r['cpc']), r['advert_id']) for r in db.query(
    "select nm_id, cpc, advert_id from wb_bid_override where account=%s", (ACC,))}

md = db.query("select max(captured_date) d from mkt_margin_control where account=%s", (ACC,))[0]['d']
marg = {r['nm_id']: (float(r['margin_own_live']) if r['margin_own_live'] is not None else None,
                     float(r['net_live']) if r['net_live'] is not None else None)
        for r in db.query("""select nm_id, margin_own_live, net_live from mkt_margin_control
                              where account=%s and captured_date=%s""", (ACC, md))}

rows = []
for nm, (cpc, adv) in bids.items():
    a = ads.get(nm) or {}
    o = org.get(nm) or {}
    sp = float(a.get('s') or 0)
    rv = float(a.get('rev') or 0)
    ad_o = int(a.get('o') or 0)
    org_o = int(o.get('orders') or 0)
    cart = int(o.get('add_to_cart') or 0)
    m, net = marg.get(nm, (None, None))
    cap = None if m is None else max(0.0, m - WB_MARGIN_GATE)      # свой потолок ДРР
    drr = (100 * sp / rv) if rv > 0 else None
    at_floor = cpc <= FLOOR + 0.01
    alive = (ad_o > 0 or org_o > 0 or cart > 0)

    # ── классификация. Порядок важен: сначала то, что решается без маржи.
    if m is None:
        color, act, why = '⬜ маржи нет', 'наблюдаем', 'нет живой закупки — судить не по чему'
    elif m < WB_MARGIN_FLOOR:
        color = '⚫ вывод'
        act = 'убрать из рекламы' if at_floor else 'на пол, затем убрать'
        why = f'маржа {m:.1f}% ниже жёсткого пола {WB_MARGIN_FLOOR:.0f}%'
    elif m < WB_MARGIN_GATE and sp > 0:
        color = '⚫ вывод'
        act = 'убрать из рекламы' if at_floor else 'на пол, затем убрать'
        why = (f'маржа {m:.1f}% ниже KPI {WB_MARGIN_GATE:.0f}% → допустимый ДРР = 0, '
               f'любой рубль рекламы уводит ниже KPI')
    elif sp >= 1.0 and not alive:
        color = '🟤 бордовый'
        act = 'на пол' if not at_floor else 'убрать из рекламы'
        why = ('ноль по всем трём сигналам при расходе' if not at_floor
               else 'мёртв уже на полу — дешевле присутствия нет, остаётся вывод')
    elif cap is not None and drr is not None and drr > cap:
        color = '🔴 красный'
        act = '−10%' if not at_floor else 'убрать из рекламы'
        why = (f'ДРР {drr:.1f}% выше своего потолка {cap:.1f}% (маржа {m:.1f}%)' if not at_floor
               else f'ДРР {drr:.1f}% выше потолка {cap:.1f}%, но ставка уже на полу')
    elif m >= WB_MARGIN_GATE and (ad_o > 0 or org_o > 0) and (drr is None or drr <= cap):
        color, act = '🟢 зелёный', '+10%'
        why = (f'заказы есть, ДРР {drr:.1f}% при потолке {cap:.1f}%' if drr is not None
               else f'заказы есть, расхода нет — потолок {cap:.1f}% свободен')
    else:
        color, act = '🟡 жёлтый', 'держим'
        why = ('в KPI, но заказов нет — наблюдаем' if sp >= 1
               else 'ставка не тратится: показы есть, кликов нет — вопрос к карточке, не к ставке')

    rows.append(dict(nm=nm, adv=adv, cpc=cpc, color=color, act=act, why=why, m=m, cap=cap,
                     drr=drr, sp=sp, rv=rv, v=int(a.get('v') or 0), c=int(a.get('c') or 0),
                     ad_o=ad_o, org_o=org_o, cart=cart, oc=int(o.get('open_card') or 0),
                     net=net, at_floor=at_floor,
                     pos=float(o['avg_position']) if o.get('avg_position') else None))

order = ['🟢 зелёный', '🟡 жёлтый', '🔴 красный', '🟤 бордовый', '⚫ вывод', '⬜ маржи нет']
print(f"НЕДЕЛЯ 10–16.08.2026 · {ACC} · {len(rows)} SKU со ставкой · маржа на {md}")
print(f"{'цвет':14}{'SKU':>6}{'расход ₽':>10}{'выручка ₽':>11}{'ДРР':>7}"
      f"{'закз рек':>9}{'закз орг':>9}{'ставка ср':>10}{'действие':>22}")
tot_s = tot_r = 0
for k in order:
    g = [r for r in rows if r['color'] == k]
    if not g:
        continue
    s = sum(r['sp'] for r in g)
    rv = sum(r['rv'] for r in g)
    tot_s += s
    tot_r += rv
    acts = {}
    for r in g:
        acts[r['act']] = acts.get(r['act'], 0) + 1
    act = ' / '.join(f"{k2}×{v}" for k2, v in sorted(acts.items(), key=lambda x: -x[1]))
    print(f"{k:14}{len(g):>6}{s:>10,.0f}{rv:>11,.0f}{(100*s/rv if rv else 0):>6.1f}%"
          f"{sum(r['ad_o'] for r in g):>9}{sum(r['org_o'] for r in g):>9}"
          f"{sum(r['cpc'] for r in g)/len(g):>10.2f}{act[:22]:>22}")
print(f"{'ИТОГО':14}{len(rows):>6}{tot_s:>10,.0f}{tot_r:>11,.0f}"
      f"{(100*tot_s/tot_r if tot_r else 0):>6.1f}%")

out = [r for r in rows if r['color'] == '⚫ вывод']
print(f"\nна вывод {len(out)}: уже на полу {sum(1 for r in out if r['at_floor'])} (только удаление), "
      f"выше пола {sum(1 for r in out if not r['at_floor'])}; их расход {sum(r['sp'] for r in out):,.0f} ₽/нед, "
      f"выручка {sum(r['rv'] for r in out):,.0f} ₽")
lo = [r for r in out if r['m'] is not None and r['m'] < WB_MARGIN_FLOOR]
print(f"  из них маржа < {WB_MARGIN_FLOOR:.0f}%: {len(lo)} · маржа {WB_MARGIN_FLOOR:.0f}–{WB_MARGIN_GATE:.0f}%: {len(out)-len(lo)}")
gr = [r for r in rows if r['color'] == '🟢 зелёный']
if gr:
    head = sum(max(0.0, r['cap']) / 100 * r['rv'] for r in gr) - sum(r['sp'] for r in gr)
    print(f"зелёные: выручка {sum(r['rv'] for r in gr):,.0f} ₽ на расход {sum(r['sp'] for r in gr):,.0f} ₽; "
          f"свободный запас до своих потолков ≈ {head:,.0f} ₽/нед")

p = '/opt/mp-analytics/docs/reports/mkt_roy_week_2026-08-16.csv'
with io.open(p, 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.writer(f, delimiter=';')
    w.writerow(['nm_id', 'advert_id', 'цвет', 'действие', 'причина', 'ставка_₽', 'маржа_live_%',
                'ДРР_факт_%', 'ДРР_потолок_%', 'показы', 'клики', 'расход_₽', 'выручка_₽',
                'закз_реклама', 'закз_органика', 'орг_корзина', 'орг_открытий', 'орг_позиция'])
    for r in sorted(rows, key=lambda r: (order.index(r['color']), -r['sp'])):
        w.writerow([r['nm'], r['adv'], r['color'], r['act'], r['why'], f"{r['cpc']:.2f}",
                    f"{r['m']:.1f}" if r['m'] is not None else '',
                    f"{r['drr']:.1f}" if r['drr'] is not None else '',
                    f"{r['cap']:.1f}" if r['cap'] is not None else '',
                    r['v'], r['c'], f"{r['sp']:.2f}", f"{r['rv']:.2f}",
                    r['ad_o'], r['org_o'], r['cart'], r['oc'],
                    f"{r['pos']:.0f}" if r['pos'] else ''])
print(f"\nполный список: {p}")

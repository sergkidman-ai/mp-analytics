#!/usr/bin/env python3
# поток: mkt (mkt-ozon)
"""ozon_ramp_effect.py — что дал разгон ставок на acc1: до / после, по каждому SKU.

Разгон идёт с 08.08 (mkt_ozon_bid_step_log: шаги 08, 10, 11, 12, 13.08; средняя ставка
когорты 11 → 18 ₽). Вопрос Сергея: какие позиции начали давать результат, а какие нет
и их надо понижать. Здесь это считается лобовым сравнением двух окон по посуточной
витрине mkt_ozon_ads_sku_daily.

Окна (нормируются на число ФАКТИЧЕСКИХ дней с данными, не на календарь):
  ДО    — с начала витрины по 07.08 включительно (последний день перед первым шагом);
  ПОСЛЕ — с 09.08 (первый полный день после шага 08.08) по последний день витрины.
Дни, за которые отчёт Ozon не собрался, в витрине отсутствуют (на 17.08 это 13.08) —
поэтому «после» короче и всё считается в пересчёте на день; пропуски перечислены в шапке.

Органика в CSV есть (недели 27.07–03.08 и 03.08–10.08), но эффект разгона по ней НЕ виден:
разгон начался 08.08, то есть на «послед» неделю приходится 2 дня из 7, а недели 10–17.08
в базе ещё нет (лаг Ozon ~2 дня). Колонки d_org_* — фон, не результат.

Контроль обязателен: те же окна считаются для не-разгоняемых SKU acc1 и для всего acc2
(там разгона не было). Без контроля любое движение спишется на разгон.

И ещё одна ловушка: окно «после» перекошено по выходным — 3 выходных из 7 дней против
2 из 12 в «до». На Ozon выходные тише буден, поэтому по календарным дням получается
мнимая «просадка рынка» −15 %, а на будних днях те же продажи, наоборот, +5 %.
Поэтому контроль печатается двумя блоками: по всем дням и только по будням; смотреть
надо на будний. Флаг --weekdays пересчитывает по будням вообще всё, включая вердикты
(но для вердиктов полное окно надёжнее: «ноль заказов» за 7 дней весомее, чем за 4).

Классы (колонка verdict):
  WIN        — после разгона есть рекламные заказы и ДРР в норме → ставку держать/поднимать;
  TRAFFIC    — трафик вырос заметно, заказов пока нет → наблюдать, решение через неделю;
  LOSE       — расход вырос, заказов нет и товар сам не продаётся → понижать/снимать;
  OVERPAY    — заказы есть, но ДРР выше нормы или ставка выше потолка → понижать до потолка;
  STUCK      — ставку подняли, а показов не прибавилось → аукцион не берётся, не в ставке дело;
  QUIET      — движения нет ни там ни там (расход около нуля) → безразлично.

Оговорка про значимость: на acc1 6–32 рекламных заказа в день на 1 163 разгоняемых SKU.
Это значит, что по ОДНОМУ SKU вывод почти всегда статистически пустой; надёжны только
агрегаты по классам и по когорте целиком. В CSV поэтому есть колонка n_days_post.

Скрипт НИЧЕГО не отправляет на площадку: только CSV в docs/reports/ и сводка.
"""
import argparse
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, '/opt/mp-analytics')
from core import db

OUT = '/opt/mp-analytics/docs/reports'
PPO_RATE = 0.05
MARGIN_HAIRCUT = 10.6      # бриф п.34: margin_own_live завышена
KPI_MARGIN = 17.0
DRR_LIMIT = 15.0
SPLIT = dt.date(2026, 8, 8)     # день первого шага разгона
WD = ''                         # заполняется --weekdays: фильтр «только будни»

f2 = lambda x: float(x or 0)


def price_bucket(p):
    p = f2(p)
    return 1 if p < 1000 else 2 if p < 2000 else 3 if p < 5000 else 4 if p < 10000 else 5


CR = {1: 0.06, 2: 0.05, 3: 0.04, 4: 0.03, 5: 0.02}


def fetch(acc):
    days = [r['d'] for r in db.query(
        "SELECT DISTINCT stat_date d FROM mkt_ozon_ads_sku_daily WHERE account=%s ORDER BY 1", (acc,))]
    if WD:
        days = [d for d in days if d.weekday() < 5]
    pre_days = [d for d in days if d <= SPLIT - dt.timedelta(days=1)]
    post_days = [d for d in days if d >= SPLIT + dt.timedelta(days=1)]
    weeks = [r['period_start'] for r in db.query("""
        SELECT DISTINCT period_start FROM ozon_search_product
         WHERE account=%s AND period_end - period_start >= 6 ORDER BY 1 DESC LIMIT 4""", (acc,))]
    w_post = weeks[0] if weeks else None
    w_pre = weeks[1] if len(weeks) > 1 else None

    rows = db.query("""
    WITH ramp AS (
        SELECT sku::text sku, max(offer_id) offer_id, min(bid_start) bid_start,
               max(bid_target) bid_target, max(bid_current) bid_current,
               string_agg(DISTINCT status, ',') status, max(grp) grp
          FROM mkt_ozon_bid_ramp WHERE account=%(a)s GROUP BY 1),
    steps AS (
        SELECT sku::text sku, count(*) FILTER (WHERE applied) n_steps,
               min(step_date) FILTER (WHERE applied) first_step,
               max(step_date) FILTER (WHERE applied) last_step
          FROM mkt_ozon_bid_step_log WHERE account=%(a)s GROUP BY 1),
    pre AS (
        SELECT sku::text sku, sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) ord, sum(orders_money) rev,
               avg(bid) FILTER (WHERE bid > 0) bid_avg,
               count(DISTINCT stat_date) n_days
          FROM mkt_ozon_ads_sku_daily
         WHERE account=%(a)s AND stat_date <= %(p1)s """ + WD + """ GROUP BY 1),
    post AS (
        SELECT sku::text sku, sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) ord, sum(orders_money) rev,
               avg(bid) FILTER (WHERE bid > 0) bid_avg,
               count(DISTINCT stat_date) n_days
          FROM mkt_ozon_ads_sku_daily
         WHERE account=%(a)s AND stat_date >= %(p2)s """ + WD + """ GROUP BY 1),
    bidnow AS (
        SELECT sku::text sku, max(bid) bid
          FROM ozon_bids WHERE account=%(a)s
           AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(a)s)
         GROUP BY 1),
    sp_pre AS (
        SELECT sku::text sku, sum(unique_view_users) views, sum(order_count) ord,
               sum(gmv) gmv, avg(position) FILTER (WHERE position>0) pos
          FROM ozon_search_product WHERE account=%(a)s AND period_start=%(wpre)s GROUP BY 1),
    sp_post AS (
        SELECT sku::text sku, sum(unique_view_users) views, sum(order_count) ord,
               sum(gmv) gmv, avg(position) FILTER (WHERE position>0) pos
          FROM ozon_search_product WHERE account=%(a)s AND period_start=%(wpost)s GROUP BY 1),
    sold AS (
        SELECT pr->>'sku' sku,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at::date <= %(p1)s
                        AND p.in_process_at::date > %(p1)s::date - 9
                        THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev_pre,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at::date >= %(p2)s
                        THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev_post,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at >= current_date - 90
                        THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev90,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at >= current_date - 90
                        THEN (pr->>'quantity')::numeric ELSE 0 END) qty90
          FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
         WHERE p.account=%(a)s GROUP BY 1),
    marg AS (
        SELECT sku::text sku, our_price, margin_own_live, verdict
          FROM mkt_ozon_margin_control WHERE account=%(a)s
           AND captured_date=(SELECT max(captured_date) FROM mkt_ozon_margin_control
                               WHERE account=%(a)s))
    SELECT r.sku, coalesce(r.offer_id, pt.offer_id) offer_id, coalesce(pt.name,'') name,
           r.bid_start, r.bid_target, r.bid_current, r.status, r.grp,
           coalesce(st.n_steps,0) n_steps, st.first_step, st.last_step,
           coalesce(pre.views,0) pre_views, coalesce(pre.clicks,0) pre_clicks,
           coalesce(pre.spend,0) pre_spend, coalesce(pre.ord,0) pre_ord,
           coalesce(pre.rev,0) pre_rev, pre.bid_avg pre_bid, coalesce(pre.n_days,0) pre_days,
           coalesce(po.views,0) post_views, coalesce(po.clicks,0) post_clicks,
           coalesce(po.spend,0) post_spend, coalesce(po.ord,0) post_ord,
           coalesce(po.rev,0) post_rev, po.bid_avg post_bid, coalesce(po.n_days,0) post_days,
           bn.bid bid_now,
           coalesce(spa.views,0) org_views_pre, coalesce(spa.ord,0) org_ord_pre,
           coalesce(spa.gmv,0) org_gmv_pre, spa.pos org_pos_pre,
           coalesce(spb.views,0) org_views_post, coalesce(spb.ord,0) org_ord_post,
           coalesce(spb.gmv,0) org_gmv_post, spb.pos org_pos_post,
           coalesce(so.rev_pre,0) sale_rev_pre, coalesce(so.rev_post,0) sale_rev_post,
           coalesce(so.rev90,0) rev90, coalesce(so.qty90,0) qty90,
           m.our_price, m.margin_own_live, coalesce(m.verdict,'') verdict,
           coalesce(pt.is_archived,false) is_archived
      FROM ramp r
      LEFT JOIN steps  st ON st.sku = r.sku
      LEFT JOIN pre       ON pre.sku = r.sku
      LEFT JOIN post   po ON po.sku = r.sku
      LEFT JOIN bidnow bn ON bn.sku = r.sku
      LEFT JOIN sp_pre spa ON spa.sku = r.sku
      LEFT JOIN sp_post spb ON spb.sku = r.sku
      LEFT JOIN sold   so ON so.sku = r.sku
      LEFT JOIN marg   m  ON m.sku  = r.sku
      LEFT JOIN ozon_product pt ON pt.account=%(a)s AND pt.sku::text = r.sku
    """, {'a': acc, 'p1': pre_days[-1] if pre_days else SPLIT,
          'p2': post_days[0] if post_days else SPLIT,
          'wpre': w_pre, 'wpost': w_post})

    npre, npost = len(pre_days), len(post_days)
    for r in rows:
        r = r
        pd_, sd = max(npre, 1), max(npost, 1)
        for k in ('views', 'clicks', 'spend', 'ord', 'rev'):
            r[f'pre_{k}_d'] = round(f2(r[f'pre_{k}']) / pd_, 3)
            r[f'post_{k}_d'] = round(f2(r[f'post_{k}']) / sd, 3)
        r['d_clicks'] = round(r['post_clicks_d'] - r['pre_clicks_d'], 3)
        r['d_spend'] = round(r['post_spend_d'] - r['pre_spend_d'], 2)
        r['d_views'] = round(r['post_views_d'] - r['pre_views_d'], 2)
        r['x_clicks'] = round(r['post_clicks_d'] / r['pre_clicks_d'], 2) if r['pre_clicks_d'] else None
        r['bid_up_fact'] = (round(f2(r['post_bid']) - f2(r['pre_bid']), 1)
                            if r['post_bid'] and r['pre_bid'] else None)
        r['post_drr'] = round(100 * f2(r['post_spend']) / f2(r['post_rev']), 1) if f2(r['post_rev']) else None
        r['pre_drr'] = round(100 * f2(r['pre_spend']) / f2(r['pre_rev']), 1) if f2(r['pre_rev']) else None
        r['post_cpc'] = round(f2(r['post_spend']) / f2(r['post_clicks']), 1) if f2(r['post_clicks']) else None
        mg = f2(r['margin_own_live']) - MARGIN_HAIRCUT if r['margin_own_live'] is not None else None
        r['margin_safe'] = round(mg, 1) if mg is not None else None
        pr = f2(r['our_price'])
        cr = CR[price_bucket(pr)]
        r['cr_bucket'] = cr
        r['ceiling_safe'] = round((pr * mg / 100 - pr * PPO_RATE) * cr, 1) if (mg and pr) else None
        bid = f2(r['bid_now']) or f2(r['bid_current'])
        r['bid_ref'] = bid
        r['headroom'] = (round(r['ceiling_safe'] - bid, 1)
                         if r['ceiling_safe'] is not None and bid else None)
        r['d_org_views'] = f2(r['org_views_post']) - f2(r['org_views_pre'])
        r['d_org_pos'] = (round(f2(r['org_pos_post']) - f2(r['org_pos_pre']), 1)
                          if r['org_pos_pre'] and r['org_pos_post'] else None)
    return rows, pre_days, post_days, w_pre, w_post


def classify(r):
    """Вердикт + причина. Порядок веток = приоритет действия."""
    post_sp = r['post_spend_d']
    pre_sp = r['pre_spend_d']
    ords = f2(r['post_ord'])
    drr = r['post_drr']
    over = r['headroom'] is not None and r['headroom'] < 0
    why = []
    if post_sp < 0.5 and pre_sp < 0.5:
        return 'QUIET', 'расход около нуля и до, и после — разгон ни на что не повлиял'
    if ords > 0 and (drr is None or drr <= DRR_LIMIT) and not over:
        why.append(f"{ords:.0f} рекл. заказов на {f2(r['post_rev']):,.0f} ₽".replace(',', ' '))
        if drr is not None:
            why.append(f'ДРР {drr} %')
        if r['headroom'] is not None:
            why.append(f"до потолка ещё {r['headroom']} ₽")
        return 'WIN', '; '.join(why)
    if ords > 0:
        if drr is not None and drr > DRR_LIMIT:
            why.append(f'ДРР {drr} % при норме {DRR_LIMIT:.0f}')
        if over:
            why.append(f"ставка {r['bid_ref']:.0f} ₽ выше потолка {r['ceiling_safe']} ₽")
        return 'OVERPAY', '; '.join(why)
    # заказов нет
    grew = (r['post_views_d'] > max(r['pre_views_d'] * 1.3, r['pre_views_d'] + 5)
            or r['d_clicks'] >= 0.3)
    if not grew and post_sp <= pre_sp + 0.5:
        return 'STUCK', (f"ставка +{r['bid_up_fact']} ₽, показов "
                         f"{r['post_views_d']:.0f}/дн против {r['pre_views_d']:.0f}/дн — "
                         f'аукцион не берётся')
    if f2(r['post_clicks']) == 0:
        return 'STUCK', (f"{r['post_views_d']:.0f} показов/дн и ни одного клика — "
                         f'проблема в карточке, не в ставке')
    if f2(r['qty90']) > 0 or f2(r['sale_rev_post']) > 0:
        return 'TRAFFIC', (f"кликов {r['post_clicks_d']:.2f}/дн против {r['pre_clicks_d']:.2f}/дн, "
                           f"рекл. заказов нет, но товар продаётся сам "
                           f"({f2(r['rev90']):,.0f} ₽/90 дн)".replace(',', ' '))
    return 'LOSE', (f"расход {post_sp:.1f} ₽/дн против {pre_sp:.1f} ₽/дн, "
                    f"{f2(r['post_clicks']):.0f} кликов и 0 заказов, продаж за 90 дн нет")


def control(pre_days, post_days, wd=''):
    """Те же окна для не-разгоняемых SKU acc1 и для acc2 — чтобы отделить разгон от рынка."""
    rows = db.query("""
    WITH ramp AS (SELECT DISTINCT sku::text sku FROM mkt_ozon_bid_ramp WHERE account='oz_acc1'),
    d AS (
      SELECT CASE WHEN a.account='oz_acc1' AND r.sku IS NOT NULL THEN '1 acc1 разгон'
                  WHEN a.account='oz_acc1' THEN '2 acc1 остальные'
                  ELSE '3 acc2 (разгона не было)' END grp,
             CASE WHEN a.stat_date <= %(p1)s THEN 'до' ELSE 'после' END w,
             a.stat_date, a.money_spent, a.clicks, a.orders_qty, a.orders_money
        FROM mkt_ozon_ads_sku_daily a
        LEFT JOIN ramp r ON r.sku = a.sku::text AND a.account='oz_acc1'
       WHERE (a.stat_date <= %(p1)s OR a.stat_date >= %(p2)s) """ + wd + """)
    SELECT grp, w, round(sum(money_spent)/count(DISTINCT stat_date)) sp_d,
           round(sum(clicks)::numeric/count(DISTINCT stat_date)) cl_d,
           round(sum(orders_qty)::numeric/count(DISTINCT stat_date),1) or_d,
           round(sum(orders_money)/count(DISTINCT stat_date)) rev_d,
           round(sum(money_spent)/nullif(sum(clicks),0),1) cpc
      FROM d GROUP BY 1,2 ORDER BY 1, 2""",
                    {'p1': pre_days[-1], 'p2': post_days[0]})
    sales = db.query("""
      SELECT p.account acc, CASE WHEN p.in_process_at::date <= %(p1)s THEN 'до' ELSE 'после' END w,
             round(sum((pr->>'price')::numeric*(pr->>'quantity')::numeric)
                   / count(DISTINCT p.in_process_at::date)) rev_d
        FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
       WHERE p.status<>'cancelled'
         AND p.in_process_at::date BETWEEN %(p0)s AND %(p3)s
         """ + wd.replace('a.stat_date', 'p.in_process_at::date') + """
       GROUP BY 1,2 ORDER BY 1,2""",
                     {'p1': pre_days[-1], 'p0': pre_days[0], 'p3': post_days[-1]})
    return rows, sales


ORDER = ['LOSE', 'OVERPAY', 'STUCK', 'TRAFFIC', 'WIN', 'QUIET']
LABEL = {
    'WIN': 'дал результат — держать/поднимать',
    'TRAFFIC': 'трафик пошёл, заказов пока нет — наблюдать',
    'OVERPAY': 'заказы есть, но переплата — понижать до потолка',
    'LOSE': 'денег больше, толку нет — понижать/снимать',
    'STUCK': 'ставку подняли, показов не прибавилось — дело не в ставке',
    'QUIET': 'без движения',
}


def write(name, rows, header):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    if not rows:
        open(path, 'w').write('# ' + header + '\n# пусто\n')
        return path
    cols = list(rows[0].keys())
    with open(path, 'w', newline='') as fh:
        fh.write('# ' + header + '\n')
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='oz_acc1')
    ap.add_argument('--weekdays', action='store_true',
                    help='считать только по будням: окно «после» перекошено по выходным '
                         '(3 из 7 против 2 из 12 в «до»), это одно смещает все итоги')
    a = ap.parse_args()
    global WD
    if a.weekdays:
        WD = 'AND extract(isodow from stat_date) <= 5'
    acc = a.account
    short = acc.replace('oz_', '')

    rows, pre_days, post_days, w_pre, w_post = fetch(acc)
    for r in rows:
        r['verdict_ramp'], r['why'] = classify(r)

    span = {post_days[0] + dt.timedelta(i) for i in range((post_days[-1] - post_days[0]).days + 1)}
    miss = sorted(span - set(post_days))
    gap = (', нет ' + ', '.join(d.strftime('%d.%m') for d in miss)) if miss else ''
    hdr = (f'{acc}: эффект разгона ставок. ДО {pre_days[0]}..{pre_days[-1]} ({len(pre_days)} дн) / '
           f'ПОСЛЕ {post_days[0]}..{post_days[-1]} ({len(post_days)} дн{gap}); '
           f'органика неделя {w_pre} → {w_post}; ставки на {dt.date.today()}; '
           f'маржа = margin_own_live − {MARGIN_HAIRCUT} п.п.; построено {dt.date.today()}')

    rows.sort(key=lambda r: (ORDER.index(r['verdict_ramp']), -r['post_spend_d']))
    p_all = write(f'ozon_{short}_ramp_effect.csv', rows, hdr + ' — вся когорта разгона')
    down = [r for r in rows if r['verdict_ramp'] in ('LOSE', 'OVERPAY')]
    p_down = write(f'ozon_{short}_ramp_down.csv', down, hdr + ' — ПОНИЖАТЬ')
    up = [r for r in rows if r['verdict_ramp'] == 'WIN']
    p_up = write(f'ozon_{short}_ramp_win.csv', up, hdr + ' — РАБОТАЕТ, можно дальше')

    n = lambda x: f'{x:,.0f}'.replace(',', ' ')
    S = lambda lst, k: sum(f2(r[k]) for r in lst)
    print(hdr)
    print(f'\nкогорта разгона: {len(rows)} SKU '
          f"(running {sum(1 for r in rows if 'running' in (r['status'] or ''))}, "
          f"paused {sum(1 for r in rows if 'paused' in (r['status'] or ''))}, "
          f"done {sum(1 for r in rows if 'done' in (r['status'] or ''))})")
    bids = [(f2(r['pre_bid']), f2(r['post_bid'])) for r in rows if r['pre_bid'] and r['post_bid']]
    if bids:
        print(f'фактическая ставка по витрине: {sum(b[0] for b in bids)/len(bids):.1f} ₽ → '
              f'{sum(b[1] for b in bids)/len(bids):.1f} ₽ ({len(bids)} SKU с данными в обоих окнах)')
    print(f'\nв день, вся когорта:')
    print(f"  расход   {S(rows,'pre_spend_d'):>9,.0f} ₽ → {S(rows,'post_spend_d'):>9,.0f} ₽".replace(',', ' '))
    print(f"  клики    {S(rows,'pre_clicks_d'):>9,.0f}   → {S(rows,'post_clicks_d'):>9,.0f}".replace(',', ' '))
    print(f"  заказы   {S(rows,'pre_ord_d'):>9,.1f}   → {S(rows,'post_ord_d'):>9,.1f}".replace(',', ' '))
    print(f"  выручка  {S(rows,'pre_rev_d'):>9,.0f} ₽ → {S(rows,'post_rev_d'):>9,.0f} ₽".replace(',', ' '))
    pd_ = 100 * S(rows, 'pre_spend') / S(rows, 'pre_rev') if S(rows, 'pre_rev') else 0
    sd = 100 * S(rows, 'post_spend') / S(rows, 'post_rev') if S(rows, 'post_rev') else 0
    print(f'  ДРР по когорте: {pd_:.1f} % → {sd:.1f} %')
    print('\nпо вердиктам:')
    for v in ORDER:
        g = [r for r in rows if r['verdict_ramp'] == v]
        if not g:
            continue
        print(f'  {v:<8} {len(g):>5} SKU | расход {n(S(g,"post_spend_d"))} ₽/дн '
              f'(было {n(S(g,"pre_spend_d"))}) | заказы {S(g,"post_ord"):.0f} '
              f'на {n(S(g,"post_rev"))} ₽ | продажи 90 дн {n(S(g,"rev90"))} ₽ — {LABEL[v]}')
    # Контроль печатается ДВАЖДЫ. Окно «после» перекошено по выходным (3 из 7 против 2 из 12
    # в «до»), а выходные на Ozon тише буден — по календарным дням это одно даёт мнимую
    # «просадку рынка». Будний срез снимает перекос; расхождение между блоками = вес выходных.
    for lbl, wd in (('по всем дням', ''),
                    ('только будни', 'AND extract(isodow from a.stat_date) <= 5')):
        cr, sales = control(pre_days, post_days, wd)
        print(f'\nконтроль, {lbl} (в день, реклама):')
        for r in cr:
            print(f"  {r['grp']:<24} {r['w']:<6} расход {n(f2(r['sp_d']))} ₽ | клики {f2(r['cl_d']):.0f}"
                  f" | CPC {r['cpc']} ₽ | заказы {f2(r['or_d']):.1f} | рекл. выручка {n(f2(r['rev_d']))} ₽")
        print(f'контроль, {lbl} (в день, ФАКТ продаж по постингам):')
        for r in sales:
            print(f"  {r['acc']:<24} {r['w']:<6} {n(f2(r['rev_d']))} ₽/дн")

    print(f'\nПОНИЖАТЬ: {len(down)} SKU, сейчас {n(S(down,"post_spend_d"))} ₽/дн '
          f'(~{n(S(down,"post_spend_d")*30)} ₽/мес) → {p_down}')
    print(f'РАБОТАЕТ: {len(up)} SKU, {n(S(up,"post_spend_d"))} ₽/дн, '
          f'реклама дала {n(S(up,"post_rev"))} ₽ за окно → {p_up}')
    print(f'вся когорта → {p_all}')


if __name__ == '__main__':
    main()

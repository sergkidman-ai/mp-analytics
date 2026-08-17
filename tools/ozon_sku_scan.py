#!/usr/bin/env python3
# поток: mkt (mkt-ozon)
"""ozon_sku_scan.py — полный разбор ВСЕХ живых SKU аккаунта Ozon: реклама + органика + продажи.

Зачем: до сих пор списки строились по одному срезу (только реклама — ozon_acc2_plan.py,
только фразы — ozon_query_econ.py). Здесь один паспорт на SKU, где рядом стоят
рекламный трафик, поисковый (органический) трафик, факт продаж и экономика. На этом
паспорте режутся три рабочих списка:

  bid_up     — метрики хорошие, ставка ниже потолка: кандидаты на подъём;
  no_traffic — трафика нет и НИКОГДА не было (в пределах имеющейся истории);
  ad_eaters  — трафик есть, реклама ест деньги без заказов.

Важно про горизонты (пишутся в шапку каждого CSV, это не одинаковые окна):
  реклама      — посуточная витрина mkt_ozon_ads_sku_daily, всё что есть (с 27.07.2026);
  поиск        — ozon_search_product, полные недели (с 22.06.2026), лаг Ozon ~2 дня;
  продажи      — raw_ozon_posting, вся глубина (с 01.05.2026);
  экономика    — mkt_ozon_margin_control, последний снимок.
«Никогда» = «ни разу за эту историю», не «за всё время жизни карточки».

Маржа из mkt_ozon_margin_control завышена (бриф п.34: на acc1 разрыв 10,6 п.п. за июль),
поэтому потолок CPC считается в двух вариантах: ceiling_live (как есть) и ceiling_safe
(маржа минус MARGIN_HAIRCUT). Решения принимать по ceiling_safe.

На площадку скрипт НИЧЕГО не отправляет: только CSV в docs/reports/ и сводка в stdout.
"""
import argparse
import csv
import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, '/opt/mp-analytics')
from core import db

OUT = '/opt/mp-analytics/docs/reports'
PPO_RATE = 0.05          # оплата за заказ, оба аккаунта с 09-10.08
MARGIN_HAIRCUT = 10.6    # п.п., поправка к margin_own_live (бриф п.34)
KPI_MARGIN = 17.0        # ниже — рекламу не разгоняем
SEARCH_RECENT_WEEKS = 4

f2 = lambda x: float(x or 0)


def price_bucket(p):
    p = f2(p)
    return 1 if p < 1000 else 2 if p < 2000 else 3 if p < 5000 else 4 if p < 10000 else 5


def fetch(acc):
    """Один проход по БД: словарь sku -> паспорт."""
    win = db.query("""SELECT min(stat_date) d0, max(stat_date) d1, count(DISTINCT stat_date) n
                        FROM mkt_ozon_ads_sku_daily WHERE account=%s""", (acc,))[0]
    ads_days = int(win['n'] or 0)
    weeks = [r['period_start'] for r in db.query("""
        SELECT DISTINCT period_start FROM ozon_search_product
         WHERE account=%s AND period_end - period_start >= 6
         ORDER BY period_start DESC LIMIT %s""", (acc, SEARCH_RECENT_WEEKS))]
    week_from = min(weeks) if weeks else dt.date.today()
    today = dt.date.today()

    rows = db.query("""
    WITH ads AS (
        SELECT sku::text sku,
               sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) ad_orders, sum(orders_money) ad_rev,
               count(DISTINCT stat_date) FILTER (WHERE views > 0) days_shown,
               count(DISTINCT campaign_id) camps
          FROM mkt_ozon_ads_sku_daily WHERE account=%(a)s GROUP BY 1),
    ads14 AS (
        SELECT sku::text sku, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) ad_orders, sum(orders_money) ad_rev
          FROM mkt_ozon_ads_sku_daily
         WHERE account=%(a)s AND stat_date >= current_date - 14 GROUP BY 1),
    bids AS (
        SELECT sku::text sku, max(bid) bid, count(DISTINCT campaign_id) n_camp,
               string_agg(DISTINCT campaign_title, ' / ') camp_titles
          FROM ozon_bids
         WHERE account=%(a)s
           AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(a)s)
         GROUP BY 1),
    srch AS (
        SELECT sku::text sku,
               sum(unique_search_users) demand, sum(unique_view_users) views,
               sum(order_count) orders, sum(gmv) gmv,
               avg(position) FILTER (WHERE position > 0) pos_avg,
               min(position) FILTER (WHERE position > 0) pos_best
          FROM ozon_search_product
         WHERE account=%(a)s AND period_start >= %(wf)s GROUP BY 1),
    srch_all AS (
        SELECT sku::text sku, sum(unique_view_users) views_all, sum(order_count) orders_all,
               sum(unique_search_users) demand_all, count(*) weeks_seen
          FROM ozon_search_product WHERE account=%(a)s GROUP BY 1),
    post AS (
        SELECT pr->>'sku' sku,
               sum(CASE WHEN p.status<>'cancelled' THEN (pr->>'quantity')::numeric ELSE 0 END) qty_all,
               sum(CASE WHEN p.status<>'cancelled'
                        THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev_all,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at >= current_date - 30
                        THEN (pr->>'quantity')::numeric ELSE 0 END) qty30,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at >= current_date - 30
                        THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev30,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at >= current_date - 90
                        THEN (pr->>'quantity')::numeric ELSE 0 END) qty90,
               sum(CASE WHEN p.status<>'cancelled' AND p.in_process_at >= current_date - 90
                        THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev90,
               count(*) n_post, count(*) FILTER (WHERE p.status='cancelled') canc,
               max(p.in_process_at) FILTER (WHERE p.status<>'cancelled')::date last_sale
          FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
         WHERE p.account=%(a)s GROUP BY 1),
    marg AS (
        SELECT sku::text sku, our_price, buyer_price, margin_own_live, net_live, volume_l,
               other_rate, verdict
          FROM mkt_ozon_margin_control
         WHERE account=%(a)s
           AND captured_date=(SELECT max(captured_date) FROM mkt_ozon_margin_control
                               WHERE account=%(a)s)),
    stock AS (
        SELECT sku::text sku, sum(free_to_sell) free_fbo
          FROM ozon_fbo_stock
         WHERE account=%(a)s
           AND captured_at=(SELECT max(captured_at) FROM ozon_fbo_stock WHERE account=%(a)s)
         GROUP BY 1)
    SELECT pt.sku::text sku, pt.offer_id, coalesce(m.verdict,'') verdict,
           coalesce(pt.name, '') name,
           coalesce(a.views,0) ad_views, coalesce(a.clicks,0) ad_clicks,
           coalesce(a.spend,0) ad_spend, coalesce(a.ad_orders,0) ad_orders,
           coalesce(a.ad_rev,0) ad_rev, coalesce(a.days_shown,0) ad_days_shown,
           coalesce(a.camps,0) ad_camps,
           coalesce(a14.clicks,0) clicks14, coalesce(a14.spend,0) spend14,
           coalesce(a14.ad_orders,0) ad_orders14, coalesce(a14.ad_rev,0) ad_rev14,
           b.bid, coalesce(b.n_camp,0) n_camp, b.camp_titles,
           coalesce(s.demand,0) s_demand, coalesce(s.views,0) s_views,
           coalesce(s.orders,0) s_orders, coalesce(s.gmv,0) s_gmv,
           s.pos_avg, s.pos_best,
           coalesce(sa.views_all,0) s_views_all, coalesce(sa.orders_all,0) s_orders_all,
           coalesce(sa.demand_all,0) s_demand_all, coalesce(sa.weeks_seen,0) s_weeks,
           coalesce(p.qty_all,0) qty_all, coalesce(p.rev_all,0) rev_all,
           coalesce(p.qty30,0) qty30, coalesce(p.rev30,0) rev30,
           coalesce(p.qty90,0) qty90, coalesce(p.rev90,0) rev90,
           coalesce(p.n_post,0) n_post, coalesce(p.canc,0) canc, p.last_sale,
           m.our_price, m.buyer_price, m.margin_own_live, m.net_live, m.volume_l,
           coalesce(st.free_fbo,0) free_fbo
      FROM ozon_product pt
      LEFT JOIN ads      a  ON a.sku  = pt.sku::text
      LEFT JOIN ads14    a14 ON a14.sku = pt.sku::text
      LEFT JOIN bids     b  ON b.sku  = pt.sku::text
      LEFT JOIN srch     s  ON s.sku  = pt.sku::text
      LEFT JOIN srch_all sa ON sa.sku = pt.sku::text
      LEFT JOIN post     p  ON p.sku  = pt.sku::text
      LEFT JOIN marg     m  ON m.sku  = pt.sku::text
      LEFT JOIN stock    st ON st.sku = pt.sku::text
     WHERE pt.account=%(a)s AND NOT pt.is_archived
    """, {'a': acc, 'wf': week_from})

    # конверсия клика в заказ по ценовым бакетам — из той же посуточной витрины
    cr_num, cr_den = {}, {}
    for r in rows:
        b = price_bucket(r['our_price'])
        cr_num[b] = cr_num.get(b, 0) + f2(r['ad_orders'])
        cr_den[b] = cr_den.get(b, 0) + f2(r['ad_clicks'])
    cr_acc = (sum(cr_num.values()) / sum(cr_den.values())) if sum(cr_den.values()) else 0.0
    cr_b = {b: (cr_num[b] / cr_den[b]) if cr_den.get(b) else cr_acc for b in cr_num}

    for r in rows:
        sp, cl, vw = f2(r['ad_spend']), f2(r['ad_clicks']), f2(r['ad_views'])
        r['cpc'] = round(sp / cl, 1) if cl else None
        r['ctr'] = round(100 * cl / vw, 2) if vw else None
        r['ad_drr'] = round(100 * sp / f2(r['ad_rev']), 1) if f2(r['ad_rev']) else None
        r['ad_cr'] = round(100 * f2(r['ad_orders']) / cl, 2) if cl else None
        r['spend_30d'] = round(sp / ads_days * 30) if ads_days else 0
        r['cancel_share'] = round(100 * f2(r['canc']) / f2(r['n_post']), 1) if f2(r['n_post']) else None
        r['days_since_sale'] = (today - r['last_sale']).days if r['last_sale'] else None
        r['in_ads'] = r['bid'] is not None
        # органический трафик = поисковые показы, не относящиеся к рекламным показам
        r['traffic_ever'] = (vw > 0 or f2(r['s_views_all']) > 0 or f2(r['qty_all']) > 0)
        mg = r['margin_own_live']
        r['margin_safe'] = round(f2(mg) - MARGIN_HAIRCUT, 1) if mg is not None else None
        cr = cr_b.get(price_bucket(r['our_price']), cr_acc)
        r['cr_bucket'] = round(cr, 4)
        pr = f2(r['our_price'])
        if mg is not None and pr:
            r['ceiling_live'] = round((pr * f2(mg) / 100 - pr * PPO_RATE) * cr, 1)
            r['ceiling_safe'] = round((pr * r['margin_safe'] / 100 - pr * PPO_RATE) * cr, 1)
        else:
            r['ceiling_live'] = r['ceiling_safe'] = None
        r['headroom'] = (round(r['ceiling_safe'] - f2(r['bid']), 1)
                         if r['ceiling_safe'] is not None and r['bid'] is not None else None)
    return rows, ads_days, win, weeks


COLS = ['sku', 'offer_id', 'name', 'in_ads', 'bid', 'n_camp', 'camp_titles',
        'ad_views', 'ad_clicks', 'ctr', 'cpc', 'ad_spend', 'spend_30d', 'ad_orders', 'ad_rev',
        'ad_drr', 'ad_cr', 'ad_days_shown', 'clicks14', 'spend14', 'ad_orders14', 'ad_rev14',
        's_demand', 's_views', 's_orders', 's_gmv', 'pos_avg', 'pos_best',
        's_views_all', 's_orders_all', 's_demand_all', 's_weeks',
        'qty30', 'rev30', 'qty90', 'rev90', 'qty_all', 'rev_all',
        'n_post', 'canc', 'cancel_share', 'last_sale', 'days_since_sale', 'free_fbo',
        'our_price', 'buyer_price', 'margin_own_live', 'margin_safe', 'net_live',
        'cr_bucket', 'ceiling_live', 'ceiling_safe', 'headroom', 'segment', 'tier', 'note']


def write(name, items, header_note):
    os.makedirs(OUT, exist_ok=True)
    path = f'{OUT}/{name}'
    with open(path, 'w', newline='') as f:
        f.write(f'# {header_note}\n')
        w = csv.writer(f)
        w.writerow(COLS)
        for r in items:
            w.writerow([r.get(c) for c in COLS])
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='oz_acc2')
    a = ap.parse_args()
    acc = a.account
    short = acc.replace('oz_', '')

    rows, ads_days, win, weeks = fetch(acc)
    hdr = (f'{acc}: реклама {win["d0"]}..{win["d1"]} ({ads_days} дн), '
           f'поиск {len(weeks)} нед с {min(weeks) if weeks else "-"}, продажи вся история, '
           f'маржа: margin_safe = margin_own_live - {MARGIN_HAIRCUT} п.п.; '
           f'построено {dt.date.today()}')

    for r in rows:
        r['segment'], r['tier'], r['note'] = '', '', ''

    # ---- сегмент 2: трафика нет и никогда не было ----------------------------
    no_traffic = [r for r in rows if not r['traffic_ever']]
    for r in no_traffic:
        r['segment'] = 'no_traffic'
        r['note'] = ('в рекламе, показов нет' if r['in_ads'] else 'вне рекламы') + \
                    (f"; спрос по фразам {f2(r['s_demand_all']):,.0f}".replace(',', ' ')
                     if f2(r['s_demand_all']) else '; спроса по фразам тоже нет')
    no_traffic.sort(key=lambda r: (-f2(r['ad_spend']), -f2(r['s_demand_all'])))

    # ---- сегмент 3: трафик есть, реклама ест --------------------------------
    # Тиры по тому, ЧТО с этим делать (разное действие — разный риск):
    #   E1 снимать      — кликов нет ни одного заказа и товар не продавался вообще;
    #   E2 снижать      — рекламных заказов нет, но товар продаётся сам (атрибуция Ozon
    #                     теряет часть заказов, поэтому снимать нельзя — только ставку вниз);
    #   E3 резать ставку— заказы есть, но ДРР выше 15 % либо ставка выше потолка;
    #   E4 склад        — заказы отменяются (≥50 %): вопрос не рекламный.
    eaters = []
    for r in rows:
        if f2(r['ad_spend']) <= 0 or f2(r['ad_clicks']) <= 0:
            continue
        no_ad_orders = f2(r['ad_orders']) == 0
        bad_drr = (r['ad_drr'] is not None and r['ad_drr'] > 15)
        over_ceiling = (r['headroom'] is not None and r['headroom'] < 0)
        cancels = (r['cancel_share'] or 0) >= 50 and f2(r['n_post']) >= 2
        if not (no_ad_orders or bad_drr or over_ceiling):
            continue
        why = []
        if no_ad_orders:
            why.append(f"{int(f2(r['ad_clicks']))} кликов, 0 рекламных заказов")
        if bad_drr:
            why.append(f"ДРР {r['ad_drr']} %")
        if over_ceiling:
            why.append(f"ставка {f2(r['bid']):.0f} ₽ > потолка {r['ceiling_safe']} ₽")
        if cancels:
            why.append(f"отмены {r['cancel_share']} %")
        if cancels:
            tier = 'E4'
        elif no_ad_orders and f2(r['qty_all']) == 0:
            tier = 'E1'
            why.append('продаж не было вовсе')
        elif no_ad_orders and f2(r['qty90']) > 0:
            tier = 'E2'
            why.append(f"но сам продаёт {f2(r['rev90']):,.0f} ₽/90 дн".replace(',', ' '))
        elif no_ad_orders:
            tier = 'E1'
            why.append(f"последняя продажа {r['last_sale']}")
        else:
            tier = 'E3'
        r['segment'] = 'ad_eater'
        r['tier'] = tier
        r['note'] = '; '.join(why)
        eaters.append(r)
    eaters.sort(key=lambda r: (r['tier'], -f2(r['ad_spend'])))

    # ---- сегмент 1: кандидаты на подъём ставки -------------------------------
    # Тиры: A — реклама уже окупается; B — товар продаётся сам, реклама есть, но слабая;
    #       C — позиция в поиске 4–30 при живом спросе (там ещё есть покупатели, бриф п.23).
    bid_up = []
    for r in rows:
        if not r['in_ads'] or r['headroom'] is None:
            continue
        if r['segment'] == 'ad_eater':
            continue  # ставку не поднимают там, где клики уже не дают заказов
        if r['margin_safe'] is None or r['margin_safe'] < KPI_MARGIN:
            continue
        if r['headroom'] <= 1:
            continue
        if (r['cancel_share'] or 0) >= 50 and f2(r['n_post']) >= 2:
            continue
        proven_ad = f2(r['ad_orders']) > 0 and (r['ad_drr'] or 999) < 15
        proven_own = f2(r['qty90']) > 0
        near_top = r['pos_avg'] is not None and 1 <= f2(r['pos_avg']) <= 30 and f2(r['s_demand']) >= 50
        if not (proven_ad or proven_own or near_top):
            continue
        why = []
        if proven_ad:
            why.append(f"реклама окупается (ДРР {r['ad_drr']} %)")
        if proven_own:
            why.append(f"сам продаёт {f2(r['rev90']):,.0f} ₽/90 дн".replace(',', ' '))
        if near_top:
            why.append(f"позиция {f2(r['pos_avg']):.0f} при спросе {f2(r['s_demand']):,.0f}".replace(',', ' '))
        r['segment'] = 'bid_up'
        r['tier'] = 'A' if proven_ad else ('B' if proven_own else 'C')
        r['note'] = '; '.join(why)
        bid_up.append(r)
    bid_up.sort(key=lambda r: (r['tier'], -(f2(r['ad_rev']) + f2(r['rev90']))))


    # ---- сегмент 4: в рекламе, но трафика не получают ------------------------
    # Ставка не берёт аукцион: товар в кампании, показов почти нет. Деньги не тратятся,
    # но и рекламы фактически нет — это «мнимое покрытие».
    stuck = [r for r in rows if r['in_ads'] and f2(r['ad_views']) < 100 and r['traffic_ever']]
    for r in stuck:
        r['segment'] = r['segment'] or 'no_impressions'
        r['tier'] = r.get('tier') or ('S0' if f2(r['ad_views']) == 0 else 'S1')
        r['note'] = (r['note'] + '; ' if r['note'] else '') + \
                    f"показов рекламы {int(f2(r['ad_views']))} за окно при ставке {f2(r['bid']):.0f} ₽"
    stuck.sort(key=lambda r: -f2(r['rev90']))

    p_all = write(f'ozon_{short}_sku_scan.csv', rows, hdr + ' — ВСЕ живые SKU')
    p1 = write(f'ozon_{short}_bid_up.csv', bid_up, hdr + ' — кандидаты на подъём ставки')
    p2 = write(f'ozon_{short}_no_traffic.csv', no_traffic, hdr + ' — трафика нет и не было')
    p3 = write(f'ozon_{short}_ad_eaters.csv', eaters, hdr + ' — трафик есть, реклама ест')
    p4 = write(f'ozon_{short}_no_impressions.csv', stuck, hdr + ' — в рекламе, но показов нет')

    n = lambda x: f'{x:,.0f}'.replace(',', ' ')
    S = lambda lst, k: sum(f2(r[k]) for r in lst)
    in_ads = [r for r in rows if r['in_ads']]
    print(hdr)
    print(f'\nживых SKU {len(rows)}, в рекламе {len(in_ads)}, расход за окно {n(S(in_ads, "ad_spend"))} ₽'
          f' (~{n(S(in_ads, "ad_spend") / ads_days * 30)} ₽/мес)')
    print(f'рекламных заказов {n(S(rows, "ad_orders"))} на {n(S(rows, "ad_rev"))} ₽;'
          f' всего продаж за 90 дн {n(S(rows, "rev90"))} ₽')
    print(f'\n1) ПОДНЯТЬ СТАВКУ: {len(bid_up)} SKU, сейчас расход {n(S(bid_up, "ad_spend"))} ₽,'
          f' реклама дала {n(S(bid_up, "ad_rev"))} ₽, продажи 90 дн {n(S(bid_up, "rev90"))} ₽ → {p1}')
    print(f'2) БЕЗ ТРАФИКА НИКОГДА: {len(no_traffic)} SKU'
          f' (из них в рекламе {sum(1 for r in no_traffic if r["in_ads"])}),'
          f' расход {n(S(no_traffic, "ad_spend"))} ₽ → {p2}')
    print(f'3) ЕДЯТ РЕКЛАМУ: {len(eaters)} SKU, расход {n(S(eaters, "ad_spend"))} ₽ за окно'
          f' (~{n(S(eaters, "spend_30d"))} ₽/мес), заказов {n(S(eaters, "ad_orders"))} → {p3}')
    print(f'4) В РЕКЛАМЕ БЕЗ ПОКАЗОВ (<100 за окно): {len(stuck)} SKU,'
          f' из них 0 показов у {sum(1 for r in stuck if f2(r["ad_views"]) == 0)} → {p4}')

    TIERS = {'A': 'реклама уже окупается', 'B': 'продаётся сам, реклама слабая',
             'C': 'позиция 1–30 при живом спросе',
             'E1': 'снимать: кликаем, не покупают, продаж не было',
             'E2': 'снижать ставку: рекламных заказов нет, но товар продаётся сам',
             'E3': 'резать ставку: ДРР выше 15 % или ставка выше потолка',
             'E4': 'склад: заказы отменяются'}
    for title, lst in (('подъём', bid_up), ('едят', eaters)):
        by = {}
        for r in lst:
            by.setdefault(r['tier'], []).append(r)
        for t in sorted(by):
            g = by[t]
            print(f'   [{title} {t}] {len(g)} SKU, расход {n(S(g, "ad_spend"))} ₽'
                  f' (~{n(S(g, "spend_30d"))} ₽/мес), продажи 90 дн {n(S(g, "rev90"))} ₽'
                  f' — {TIERS.get(t, "")}')
    print(f'паспорт всех SKU → {p_all}')


if __name__ == '__main__':
    main()

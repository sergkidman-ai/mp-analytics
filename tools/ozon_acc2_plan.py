#!/usr/bin/env python3
"""ozon_acc2_plan.py — что добавить в рекламу и что убрать по acc2 (поток mkt).

Логика повторяет разбор acc1 (tools/ozon_acc1_plan2.py, docs/BRIEF_MKT_OZON.md п.22, 28, 32),
но с двумя поправками, купленными кровью на acc1:

1. «Нет продаж» — НЕ повод считать товар мёртвым: волна 1 на acc1 оказалась не мёртвой,
   а отменяемой (100 % отмен из-за отсутствия стока). Поэтому доля отмен считается отдельно
   и товары с отменами не валятся в общий список «убрать», а идут своей строкой: там вопрос
   не рекламный, а складской.
2. Маржа берётся из mkt_ozon_margin_control, которая завышает её (см. п.34, other_rate).
   Здесь она нужна только для РАНЖИРОВАНИЯ и отсечения явно убыточного, а не как факт;
   ставки по этому файлу не назначаются.

Окна: реклама — посуточная витрина mkt_ozon_ads_sku_daily (всё, что есть), продажи — 90 дней
по raw_ozon_posting. На площадку скрипт НИЧЕГО не отправляет: только CSV в docs/reports/.
"""
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, '/opt/mp-analytics')
from core import db

ACC = 'oz_acc2'
OUT = 'docs/reports'
SALES_DAYS = 90


def rows(q, p=()):
    return db.query(q, p)


def main():
    ads_win = rows("""SELECT min(stat_date) a, max(stat_date) b, count(DISTINCT stat_date) n
                        FROM mkt_ozon_ads_sku_daily WHERE account=%s""", (ACC,))[0]
    days = int(ads_win['n'] or 0)
    since = dt.date.today() - dt.timedelta(days=SALES_DAYS)
    print(f"окно рекламы: {ads_win['a']}..{ads_win['b']} ({days} дн), продажи с {since}")

    data = rows("""
        WITH ads AS (
            SELECT sku::text sku, sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
                   sum(orders_qty) ad_orders, sum(orders_money) ad_rev, max(bid) bid,
                   string_agg(DISTINCT campaign_id::text, '/') camps
              FROM mkt_ozon_ads_sku_daily WHERE account=%(a)s GROUP BY 1),
        sold AS (
            SELECT pr->>'sku' sku,
                   sum(CASE WHEN p.status<>'cancelled' THEN (pr->>'quantity')::numeric ELSE 0 END) qty,
                   sum(CASE WHEN p.status<>'cancelled'
                            THEN (pr->>'price')::numeric*(pr->>'quantity')::numeric ELSE 0 END) rev,
                   count(*) FILTER (WHERE p.status='cancelled') canc,
                   count(*) n_post,
                   max(p.in_process_at)::date last_sale
              FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
             WHERE p.account=%(a)s AND p.in_process_at >= %(s)s GROUP BY 1),
        bids AS (
            SELECT DISTINCT sku::text sku FROM ozon_bids
             WHERE account=%(a)s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(a)s)),
        marg AS (
            SELECT sku::text sku, margin_own_live, our_price, net_live FROM mkt_ozon_margin_control
             WHERE account=%(a)s AND captured_date=(SELECT max(captured_date) FROM mkt_ozon_margin_control WHERE account=%(a)s))
        SELECT pt.sku::text sku, pt.offer_id, pt.name,
               coalesce(a.views,0) views, coalesce(a.clicks,0) clicks, coalesce(a.spend,0) spend,
               coalesce(a.ad_orders,0) ad_orders, coalesce(a.ad_rev,0) ad_rev, a.bid, a.camps,
               coalesce(s.qty,0) qty90, coalesce(s.rev,0) rev90, coalesce(s.canc,0) canc,
               coalesce(s.n_post,0) n_post, s.last_sale,
               m.margin_own_live, m.our_price, m.net_live,
               (b.sku IS NOT NULL) in_ads
          FROM ozon_product pt
          LEFT JOIN ads a ON a.sku=pt.sku::text
          LEFT JOIN sold s ON s.sku=pt.sku::text
          LEFT JOIN bids b ON b.sku=pt.sku::text
          LEFT JOIN marg m ON m.sku=pt.sku::text
         WHERE pt.account=%(a)s AND NOT pt.is_archived
    """, {'a': ACC, 's': since})

    def w(name, items, cols):
        os.makedirs(OUT, exist_ok=True)
        path = f'{OUT}/{name}'
        with open(path, 'w', newline='') as f:
            wr = csv.writer(f)
            wr.writerow(cols)
            for r in items:
                wr.writerow([r.get(c) for c in cols])
        return path

    f2 = lambda x: float(x or 0)
    for r in data:
        r['cancel_share'] = round(100 * r['canc'] / r['n_post'], 1) if r['n_post'] else None
        r['drr'] = round(100 * f2(r['spend']) / f2(r['ad_rev']), 1) if f2(r['ad_rev']) else None
        r['spend_30d'] = round(f2(r['spend']) / days * 30, 0) if days else 0

    in_ads = [r for r in data if r['in_ads']]
    # 1. УБРАТЬ: платим, и ни рекламных заказов, ни продаж вообще
    cut = sorted([r for r in in_ads if f2(r['spend']) > 0 and f2(r['ad_orders']) == 0 and f2(r['qty90']) == 0
                  and f2(r['canc']) == 0], key=lambda r: -f2(r['spend']))
    # 2. СКЛАД, а не реклама: заказы есть, но их отменяют
    canc = sorted([r for r in in_ads if r['cancel_share'] is not None and r['cancel_share'] >= 50 and r['n_post'] >= 2],
                  key=lambda r: -f2(r['spend']))
    # 3. ДОБАВИТЬ: продаётся само, в рекламе нет
    add = sorted([r for r in data if not r['in_ads'] and f2(r['qty90']) > 0
                  and (r['cancel_share'] or 0) < 50
                  and (r['margin_own_live'] is None or f2(r['margin_own_live']) > 0)],
                 key=lambda r: -f2(r['rev90']))
    # 4. Кандидаты на ставку: реклама уже окупается (ДРР < 5 %) и продажи есть
    good = sorted([r for r in in_ads if r['drr'] is not None and r['drr'] < 5 and f2(r['ad_orders']) > 0],
                  key=lambda r: -f2(r['ad_rev']))

    cols_ads = ['sku', 'offer_id', 'name', 'camps', 'bid', 'views', 'clicks', 'spend', 'spend_30d',
                'ad_orders', 'ad_rev', 'drr', 'qty90', 'rev90', 'canc', 'cancel_share', 'last_sale',
                'margin_own_live', 'our_price']
    cols_add = ['sku', 'offer_id', 'name', 'qty90', 'rev90', 'canc', 'cancel_share', 'last_sale',
                'margin_own_live', 'our_price', 'net_live']
    p1 = w('ozon_acc2_cut.csv', cut, cols_ads)
    p2 = w('ozon_acc2_stock_issue.csv', canc, cols_ads)
    p3 = w('ozon_acc2_add.csv', add, cols_add)
    p4 = w('ozon_acc2_bid_candidates.csv', good, cols_ads)

    tot_spend = sum(f2(r['spend']) for r in in_ads)
    print(f"\nв рекламе {len(in_ads)} SKU, расход за окно {tot_spend:,.0f} ₽"
          f" (~{tot_spend/days*30:,.0f} ₽/мес)".replace(',', ' '))
    print(f"УБРАТЬ: {len(cut)} SKU, {sum(f2(r['spend']) for r in cut):,.0f} ₽ за окно"
          f" (~{sum(f2(r['spend_30d']) for r in cut):,.0f} ₽/мес) → {p1}".replace(',', ' '))
    print(f"СКЛАД (отмены ≥50 %): {len(canc)} SKU, {sum(f2(r['spend']) for r in canc):,.0f} ₽ за окно → {p2}".replace(',', ' '))
    print(f"ДОБАВИТЬ: {len(add)} SKU, выручка 90 дн {sum(f2(r['rev90']) for r in add):,.0f} ₽ → {p3}".replace(',', ' '))
    print(f"  из них топ-100 дают {sum(f2(r['rev90']) for r in add[:100]):,.0f} ₽".replace(',', ' '))
    print(f"СТАВКА (ДРР<5 %): {len(good)} SKU, реклама дала {sum(f2(r['ad_rev']) for r in good):,.0f} ₽"
          f" при расходе {sum(f2(r['spend']) for r in good):,.0f} ₽ → {p4}".replace(',', ' '))


if __name__ == '__main__':
    main()

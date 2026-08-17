#!/usr/bin/env python3
# поток: mkt
"""Общий профиль аккаунта Ozon по неделям: продажи, реклама и органика ВМЕСТЕ, без деления по SKU.

Задача: увидеть, растёт аккаунт или падает, и какой ценой — сколько выручки приходится на
рекламный расход, какая доля заказов приходит из рекламы, что происходит с поисковым спросом.

Источники:
  raw_ozon_posting       — факт продаж (отправления, статус <> cancelled, дата in_process_at)
  mkt_ozon_ads_sku_daily — реклама по дням (показы, клики, расход, заказы и выручка рекламы)
  ozon_search_product    — недельный отчёт поиска (позиция, искавшие, смотревшие, GMV)

Оговорки, которые нельзя терять при чтении цифр:
  * недели считаются календарные пн–вс, поэтому вес выходных одинаков и сравнение честное;
  * отмены прилетают задним числом — свежая неделя всегда чуть завышена;
  * «органика» здесь = всё, что не приписано рекламе (заказы всего − заказы рекламы). Это НЕ
    чистый поиск: сюда попадают повторные покупки, переходы из корзины, акции;
  * отчёт поиска отстаёт на 1–2 недели и его order_count непригоден (нули во всех строках).

Запуск: venv/bin/python tools/ozon_account_profile.py --account oz_acc1 --weeks 8
"""
import argparse
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, '/opt/mp-analytics')
from core import db   # noqa: E402

OUT = '/opt/mp-analytics/docs/reports'


def f(x):
    return float(x) if x is not None else 0.0


def weeks_back(n, last_full_end=None):
    """Список понедельников последних n ПОЛНЫХ недель (последняя закончилась вчера или раньше)."""
    today = dt.date.today()
    last_mon = today - dt.timedelta(days=today.weekday())      # понедельник текущей недели
    end = last_mon - dt.timedelta(days=7)                       # понедельник последней полной
    return [end - dt.timedelta(days=7 * i) for i in range(n - 1, -1, -1)]


def fetch(acc, w0):
    w1 = w0 + dt.timedelta(days=6)
    p = {'a': acc, 'w0': w0, 'w1': w1}

    sales = db.query("""
        SELECT count(DISTINCT p.posting_number) postings,
               sum((pr->>'quantity')::numeric) qty,
               sum((pr->>'price')::numeric * (pr->>'quantity')::numeric) rev,
               count(DISTINCT pr->>'sku') sku
          FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
         WHERE p.account=%(a)s AND p.status <> 'cancelled'
           AND p.in_process_at::date BETWEEN %(w0)s AND %(w1)s""", p)[0]

    canc = db.query("""
        SELECT count(DISTINCT p.posting_number) n
          FROM raw_ozon_posting p
         WHERE p.account=%(a)s AND p.status='cancelled'
           AND p.in_process_at::date BETWEEN %(w0)s AND %(w1)s""", p)[0]['n']

    ads = db.query("""
        SELECT count(DISTINCT stat_date) days, count(DISTINCT sku) sku,
               sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
               sum(orders_qty) orders, sum(orders_money) rev
          FROM mkt_ozon_ads_sku_daily
         WHERE account=%(a)s AND stat_date BETWEEN %(w0)s AND %(w1)s""", p)[0]

    # отчёт поиска: берём неделю, которая заканчивается не позже конца нашей
    org = db.query("""
        SELECT period_start ws, count(*) rows, count(DISTINCT sku) sku,
               sum(unique_search_users) searched, sum(unique_view_users) viewed,
               sum(gmv) gmv, round(avg(position) FILTER (WHERE position > 0)::numeric, 1) pos
          FROM ozon_search_product
         WHERE account=%(a)s AND period_end - period_start >= 6
           AND period_start = (SELECT max(period_start) FROM ozon_search_product
                                WHERE account=%(a)s AND period_end - period_start >= 6
                                  AND period_end <= %(w1)s + 3)
         GROUP BY 1""", p)
    org = org[0] if org else {}

    r = {
        'week': f'{w0:%d.%m}–{w1:%d.%m}', 'w0': w0,
        'postings': int(f(sales['postings'])), 'qty': int(f(sales['qty'])),
        'rev': round(f(sales['rev'])), 'sku_sold': int(f(sales['sku'])),
        'cancelled': int(f(canc)),
        'ad_days': int(f(ads['days'])), 'ad_sku': int(f(ads['sku'])),
        'ad_views': int(f(ads['views'])), 'ad_clicks': int(f(ads['clicks'])),
        'ad_spend': round(f(ads['spend'])), 'ad_orders': round(f(ads['orders']), 1),
        'ad_rev': round(f(ads['rev'])),
        'org_week': str(org.get('ws') or ''), 'org_searched': int(f(org.get('searched'))),
        'org_viewed': int(f(org.get('viewed'))), 'org_gmv': round(f(org.get('gmv'))),
        'org_pos': org.get('pos'), 'org_sku': int(f(org.get('sku'))),
    }
    r['avg_check'] = round(r['rev'] / r['qty']) if r['qty'] else 0
    r['drr'] = round(100 * r['ad_spend'] / r['rev'], 1) if r['rev'] else None
    r['cpc'] = round(r['ad_spend'] / r['ad_clicks'], 1) if r['ad_clicks'] else None
    r['ctr'] = round(100 * r['ad_clicks'] / r['ad_views'], 2) if r['ad_views'] else None
    r['ad_cr'] = round(100 * r['ad_orders'] / r['ad_clicks'], 2) if r['ad_clicks'] else None
    r['ad_share_rev'] = round(100 * r['ad_rev'] / r['rev'], 1) if r['rev'] else None
    r['nonad_rev'] = r['rev'] - r['ad_rev']
    r['cpo'] = round(r['ad_spend'] / r['ad_orders']) if r['ad_orders'] else None
    r['canc_pct'] = round(100 * r['cancelled'] / (r['cancelled'] + r['postings']), 1) \
        if (r['cancelled'] + r['postings']) else None
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='oz_acc1')
    ap.add_argument('--weeks', type=int, default=8)
    a = ap.parse_args()

    rows = [fetch(a.account, w) for w in weeks_back(a.weeks)]
    os.makedirs(OUT, exist_ok=True)
    path = f'{OUT}/ozon_{a.account[3:]}_profile.csv'
    cols = list(rows[0].keys())
    with open(path, 'w', newline='') as fh:
        fh.write(f'# {a.account}: недельный профиль (продажи + реклама + поиск), '
                 f'календарные недели пн–вс\n')
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)

    def d(cur, prv):
        if not prv:
            return '   —'
        return f'{100 * (cur - prv) / prv:+5.0f}%'

    print(f'\n{a.account}: профиль по неделям (все SKU, продажи+реклама вместе)')
    print(f'{"неделя":<13}{"выручка":>10}{"":>7}{"штук":>7}{"":>7}{"чек":>7}'
          f'{"реклама":>9}{"":>7}{"ДРР":>6}{"дни":>5}')
    for i, r in enumerate(rows):
        pv = rows[i - 1] if i else None
        print(f'{r["week"]:<13}{r["rev"]:>10,}{d(r["rev"], pv and pv["rev"]):>7}'
              f'{r["qty"]:>7}{d(r["qty"], pv and pv["qty"]):>7}{r["avg_check"]:>7,}'
              f'{r["ad_spend"]:>9,}{d(r["ad_spend"], pv and pv["ad_spend"]):>7}'
              f'{(str(r["drr"]) + "%"):>6}{r["ad_days"]:>5}'.replace(',', ' '))
    print(f'\nфайл: {path}')


if __name__ == '__main__':
    main()

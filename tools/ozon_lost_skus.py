#!/usr/bin/env python3
# поток: mkt
"""Кто выпал из продаж: SKU, продававшиеся в базовом окне и замолчавшие в последнем.

Вопрос, на который отвечает скрипт: аккаунт сузился с ~208 продающихся SKU в неделю до ~178 —
это одни и те же товары стали продаваться реже, или конкретные позиции выпали совсем? И если
выпали — из-за остатков, цены, потери позиции в выдаче или ухода из рекламы?

Окна по умолчанию: база 15.06–12.07 (4 недели), последнее 20.07–16.08 (4 недели).
Диагноз выставляется по приоритету причин, первая сработавшая:
  нет остатка          — карточка связана с МойСкладом и там ноль (+ FBO пуст);
  ушёл из рекламы      — в базовом окне была ставка, сейчас нет;
  просел в выдаче      — средняя позиция ухудшилась более чем на 20 пунктов;
  цена выросла         — наша цена выше базовой более чем на 10 %;
  спрос упал           — показов карточки стало вдвое меньше;
  причина не видна     — товар есть, виден, цена та же, а продаж нет;
  остаток неизвестен   — карточка не связана с МойСкладом, склад проверить нечем.

Запуск: venv/bin/python tools/ozon_lost_skus.py --account oz_acc1
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


SQL = """
WITH base AS (
    SELECT pr->>'sku' sku,
           sum((pr->>'quantity')::numeric) qty,
           sum((pr->>'price')::numeric * (pr->>'quantity')::numeric) rev,
           round(avg((pr->>'price')::numeric)) price
      FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
     WHERE p.account=%(a)s AND p.status <> 'cancelled'
       AND p.in_process_at::date BETWEEN %(b0)s AND %(b1)s
     GROUP BY 1),
last AS (
    SELECT pr->>'sku' sku, sum((pr->>'quantity')::numeric) qty
      FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
     WHERE p.account=%(a)s AND p.status <> 'cancelled'
       AND p.in_process_at::date BETWEEN %(l0)s AND %(l1)s
     GROUP BY 1),
lastsale AS (
    SELECT pr->>'sku' sku, max(p.in_process_at)::date d
      FROM raw_ozon_posting p, jsonb_array_elements(p.payload->'products') pr
     WHERE p.account=%(a)s AND p.status <> 'cancelled'
     GROUP BY 1),
org_b AS (   -- органика базовой недели
    SELECT sku::text, avg(position) FILTER (WHERE position > 0) pos,
           sum(unique_view_users) views
      FROM ozon_search_product
     WHERE account=%(a)s AND period_start=%(ob)s GROUP BY 1),
org_l AS (   -- органика последней закрытой недели
    SELECT sku::text, avg(position) FILTER (WHERE position > 0) pos,
           sum(unique_view_users) views
      FROM ozon_search_product
     WHERE account=%(a)s AND period_start=%(ol)s GROUP BY 1),
ads_b AS (
    SELECT sku, sum(money_spent) spend, sum(clicks) clicks, max(bid) bid
      FROM mkt_ozon_ads_sku_daily
     WHERE account=%(a)s AND stat_date BETWEEN %(b0)s AND %(b1)s GROUP BY 1),
ads_l AS (
    SELECT sku, sum(money_spent) spend, sum(clicks) clicks, max(bid) bid
      FROM mkt_ozon_ads_sku_daily
     WHERE account=%(a)s AND stat_date BETWEEN %(l0)s AND %(l1)s GROUP BY 1),
bids AS (
    SELECT sku::text, max(bid) bid FROM ozon_bids
     WHERE account=%(a)s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(a)s)
     GROUP BY 1),
marg AS (
    SELECT sku::text, max(our_price) our_price, max(margin_own_live) margin
      FROM mkt_ozon_margin_control
     WHERE account=%(a)s AND captured_date=(SELECT max(captured_date) FROM mkt_ozon_margin_control
                                             WHERE account=%(a)s)
     GROUP BY 1),
ms AS (
    -- ВАЖНО: ключ связки карточки Ozon с МойСкладом — external_code (7190 совпадений на acc1),
    -- article совпадает лишь у 96 карточек. Раньше джойнили по article и получали «нет остатка»
    -- у всех подряд, включая товары, которые прямо сейчас продаются.
    SELECT external_code ec, article ar, sum(stock) qty FROM supplier_stock
     WHERE captured_at=(SELECT max(captured_at) FROM supplier_stock) GROUP BY 1, 2),
fbo AS (
    SELECT sku::text, sum(free_to_sell) q FROM ozon_fbo_stock
     WHERE account=%(a)s AND captured_at=(SELECT max(captured_at) FROM ozon_fbo_stock WHERE account=%(a)s)
     GROUP BY 1)
SELECT pt.sku::text sku, pt.offer_id, pt.name,
       b.qty base_qty, b.rev base_rev, b.price base_price,
       coalesce(l.qty, 0) last_qty, ls.d last_sale,
       ob.pos pos_b, ol.pos pos_l, ob.views views_b, ol.views views_l,
       ab.spend spend_b, ab.clicks clicks_b, al.spend spend_l, al.clicks clicks_l,
       ab.bid bid_b, bd.bid bid_now,
       m.our_price, m.margin, coalesce(fbo.q, 0) fbo_stock,
       (SELECT sum(qty) FROM ms WHERE ms.ec = pt.offer_id OR ms.ar = pt.offer_id) ms_stock
  FROM base b
  JOIN ozon_product pt ON pt.sku::text = b.sku AND pt.account=%(a)s
  LEFT JOIN last l      ON l.sku  = b.sku
  LEFT JOIN lastsale ls ON ls.sku = b.sku
  LEFT JOIN org_b ob    ON ob.sku = b.sku
  LEFT JOIN org_l ol    ON ol.sku = b.sku
  LEFT JOIN ads_b ab    ON ab.sku = b.sku
  LEFT JOIN ads_l al    ON al.sku = b.sku
  LEFT JOIN bids bd     ON bd.sku = b.sku
  LEFT JOIN marg m      ON m.sku  = b.sku
  LEFT JOIN fbo         ON fbo.sku = b.sku
 WHERE coalesce(l.qty, 0) = 0
"""


def diagnose(r):
    # ms_stock = None означает «карточка не связана с МойСкладом», а НЕ «остаток ноль».
    # Выгрузка supplier_stock хранит почти только позиции с остатком, поэтому по отсутствию
    # строки можно лишь подозревать пустоту, но не утверждать её.
    linked = r['ms_stock'] is not None
    stock = f(r['ms_stock']) + f(r['fbo_stock'])
    if linked and stock <= 0:
        return 'нет остатка', f"на складах пусто (МойСклад 0, FBO 0), последняя продажа {r['last_sale']}"
    if f(r['bid_b']) > 0 and not f(r['bid_now']):
        return 'ушёл из рекламы', (f"в базовом окне была ставка {f(r['bid_b']):.1f} ₽ и {f(r['clicks_b']):.0f} кликов, "
                                   f"сейчас в рекламе нет")
    dpos = f(r['pos_l']) - f(r['pos_b']) if (r['pos_b'] and r['pos_l']) else None
    if dpos is not None and dpos > 20:
        return 'просел в выдаче', (f"средняя позиция {f(r['pos_b']):.0f} → {f(r['pos_l']):.0f} "
                                   f"(+{dpos:.0f}), показы {f(r['views_b']):.0f} → {f(r['views_l']):.0f}")
    if f(r['base_price']) > 0 and f(r['our_price']) > f(r['base_price']) * 1.1:
        return 'цена выросла', (f"продавали по {f(r['base_price']):.0f} ₽, сейчас "
                                f"{f(r['our_price']):.0f} ₽ (+{100 * (f(r['our_price']) / f(r['base_price']) - 1):.0f} %)")
    if f(r['views_b']) > 0 and f(r['views_l']) < f(r['views_b']) * 0.5:
        return 'спрос упал', f"показы карточки {f(r['views_b']):.0f} → {f(r['views_l']):.0f}"
    st = f'{stock:.0f}' if linked else 'нет связки с МС'
    kind = 'причина не видна' if linked else 'остаток неизвестен'
    pb = f"{f(r['pos_b']):.0f}" if r['pos_b'] else 'нет'
    pl = f"{f(r['pos_l']):.0f}" if r['pos_l'] else 'нет'
    return kind, (f"остаток {st}, позиция {pb}→{pl}, "
                  f"показы {f(r['views_b']):.0f}→{f(r['views_l']):.0f}, "
                  f"цена {f(r['base_price']):.0f}→{f(r['our_price']):.0f} ₽")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='oz_acc1')
    ap.add_argument('--base', default='2026-06-15')     # начало базового окна
    ap.add_argument('--base-end', default='2026-07-12')
    ap.add_argument('--last', default='2026-07-20')     # начало последнего окна
    ap.add_argument('--last-end', default='2026-08-16')
    a = ap.parse_args()

    d = dt.date.fromisoformat
    p = {'a': a.account, 'b0': d(a.base), 'b1': d(a.base_end), 'l0': d(a.last), 'l1': d(a.last_end)}
    weeks = db.query("""SELECT DISTINCT period_start w FROM ozon_search_product
                         WHERE account=%(a)s AND period_end - period_start >= 6
                         ORDER BY 1""", p)
    p['ob'] = weeks[0]['w']          # первая доступная неделя поиска ≈ базовое окно
    p['ol'] = weeks[-1]['w']         # последняя закрытая

    rows = db.query(SQL, p)
    for r in rows:
        r['reason_kind'], r['reason'] = diagnose(r)
        r['dpos'] = round(f(r['pos_l']) - f(r['pos_b']), 1) if (r['pos_b'] and r['pos_l']) else None
        r['stock'] = f(r['ms_stock']) + f(r['fbo_stock'])
    rows.sort(key=lambda r: -f(r['base_rev']))

    os.makedirs(OUT, exist_ok=True)
    path = f'{OUT}/ozon_{a.account[3:]}_lost_skus.csv'
    cols = ['reason_kind', 'sku', 'offer_id', 'name', 'base_qty', 'base_rev', 'base_price',
            'our_price', 'margin', 'last_sale', 'ms_stock', 'fbo_stock',
            'pos_b', 'pos_l', 'dpos', 'views_b', 'views_l',
            'bid_b', 'bid_now', 'spend_b', 'clicks_b', 'spend_l', 'clicks_l', 'reason']
    with open(path, 'w', newline='') as fh:
        fh.write(f'# {a.account}: продавались {a.base}–{a.base_end}, молчат {a.last}–{a.last_end}; '
                 f'органика {p["ob"]} против {p["ol"]}\n')
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)

    agg = {}
    for r in rows:
        k = r['reason_kind']
        s = agg.setdefault(k, {'n': 0, 'rev': 0.0, 'qty': 0.0})
        s['n'] += 1
        s['rev'] += f(r['base_rev'])
        s['qty'] += f(r['base_qty'])
    tot_rev = sum(s['rev'] for s in agg.values())
    print(f'\n{a.account}: выпали из продаж {len(rows)} SKU '
          f'(продавали {a.base}–{a.base_end}, за {a.last}–{a.last_end} — ноль)')
    print(f'потерянная выручка базового окна: {tot_rev:,.0f} ₽'.replace(',', ' '))
    for k, s in sorted(agg.items(), key=lambda kv: -kv[1]['rev']):
        print(f'  {k:<18}{s["n"]:>4} SKU  было {s["qty"]:>5.0f} шт на {s["rev"]:>10,.0f} ₽'.replace(',', ' '))
    print(f'\nфайл: {path}')


if __name__ == '__main__':
    main()

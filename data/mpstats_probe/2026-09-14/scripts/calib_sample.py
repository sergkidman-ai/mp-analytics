# поток: mpstats-probe — поштучная калибровка выборки (by_period/full/list против факта)
import json, os, pandas as pd, numpy as np
P = '/opt/mp-analytics/data/mpstats_probe/2026-09-14/'
s = pd.read_csv(P + 'derived/calib_sample_actual.csv', dtype={'marketplace_id': str})
lists = {mp: pd.read_csv(P + f'derived/calib_list_{mp}.csv', dtype={'marketplace_id': str}).set_index('marketplace_id') for mp in ('wb', 'ozon')}
out = []
for r in s.itertuples():
    mp, mid = r.marketplace, r.marketplace_id
    row = dict(marketplace=mp, account=r.account, sku=r.sku, marketplace_id=mid, pack_qty=r.pack_qty, tier=r.sales_tier,
               actual_sales=r.actual_orders, actual_revenue=r.actual_order_sum, actual_avg_price=r.actual_avg_price,
               actual_stock_days=r.actual_stock_days, fbs_mode=r.fbs_mode,
               mpstats_sales_full=None, mpstats_sales_by_period_sum=None, mpstats_sales_list=None,
               mpstats_revenue=None, mpstats_avg_price=None, mpstats_stock_days=None, notes='')
    notes = []
    bp = P + f'raw/s2_{mp[:2] if mp=="wb" else "oz"}_bp_{mid}.json'
    if os.path.exists(bp):
        d = sorted(json.load(open(bp)), key=lambda x: x['data'])
        row['mpstats_sales_by_period_sum'] = sum(x['sales'] or 0 for x in d)
        row['mpstats_revenue'] = sum(x.get('revenue') or 0 for x in d)
        row['mpstats_stock_days'] = sum(1 for x in d if (x.get('balance') or 0) > 0)
        pr = [x['final_price'] for x in d if x.get('final_price')]
        row['mpstats_avg_price'] = round(np.mean(pr)) if pr else None
        if d and (d[0]['data'] != '2026-08-15' or d[-1]['data'] != '2026-09-13' or len(d) != 30): notes.append(f'period {d[0]["data"]}..{d[-1]["data"]} n={len(d)}')
        ups = sum(max((d[i]['balance'] or 0) - (d[i-1]['balance'] or 0), 0) for i in range(1, len(d)))
        notes.append(f'stock_ups={ups}')
        row['mp_first15'] = sum(x['sales'] or 0 for x in d[:15]); row['mp_last15'] = sum(x['sales'] or 0 for x in d[15:])
    fp = P + f'raw/s2_oz_full_{mid}.json'
    if os.path.exists(fp):
        ps = json.load(open(fp)).get('period_stats') or {}
        if isinstance(ps, list): notes.append('full: period_stats пуст (MPSTATS не видит продаж)'); ps = {'sales': 0, 'revenue': 0}
        row['mpstats_sales_full'] = ps.get('sales'); row['mpstats_revenue'] = row['mpstats_revenue'] or ps.get('revenue')
    if mid in lists[mp].index:
        L = lists[mp].loc[mid]
        row['mpstats_sales_list'] = int(L.mp_sales)
        if row['mpstats_revenue'] is None: row['mpstats_revenue'] = int(L.mp_revenue)
        if row['mpstats_stock_days'] is None: row['mpstats_stock_days'] = L.mp_stock_days
        if row['mpstats_avg_price'] is None: row['mpstats_avg_price'] = L.mp_price
    est = next((row[k] for k in ('mpstats_sales_by_period_sum', 'mpstats_sales_full', 'mpstats_sales_list') if row[k] is not None), None)
    row['mp_sales_best'] = est
    if est is not None and r.actual_orders > 0:
        row['sales_error_percent'] = round((est - r.actual_orders) / r.actual_orders * 100)
    if row['mpstats_revenue'] is not None and r.actual_order_sum > 0:
        row['revenue_error_percent'] = round((row['mpstats_revenue'] - r.actual_order_sum) / r.actual_order_sum * 100)
    vals = {k: row[k] for k in ('mpstats_sales_full', 'mpstats_sales_by_period_sum', 'mpstats_sales_list') if row[k] is not None}
    if len(set(vals.values())) > 1: notes.append('методы расходятся: ' + str(vals))
    elif len(vals) > 1: notes.append('full/by_period/list совпали')
    if est is None: notes.append('нет запроса MPSTATS (бюджет)')
    row['notes'] = '; '.join(notes)
    out.append(row)
df = pd.DataFrame(out)
cols = ['marketplace', 'account', 'sku', 'marketplace_id', 'pack_qty', 'actual_sales', 'mpstats_sales_full', 'mpstats_sales_by_period_sum', 'mpstats_sales_list',
        'actual_revenue', 'mpstats_revenue', 'actual_avg_price', 'mpstats_avg_price', 'actual_stock_days', 'mpstats_stock_days', 'fbs_mode',
        'sales_error_percent', 'revenue_error_percent', 'notes', 'tier', 'mp_sales_best', 'mp_first15', 'mp_last15']
df = df.reindex(columns=cols)
df.to_csv(P + 'derived/calibration_table.csv', index=False)
m = []
for mp in ('wb', 'ozon'):
    x = df[(df.marketplace == mp) & df.mp_sales_best.notna()]
    if not len(x): continue
    a, e = x.actual_sales, x.mp_sales_best.astype(float)
    pos = x[a > 0]
    m.append(dict(mp=mp, n=len(x), actual=a.sum(), mpstats=e.sum(), bias_x=round(e.sum() / max(a.sum(), 1), 1),
                  wape_pct=round((e - a).abs().sum() / max(a.sum(), 1) * 100),
                  median_err_pct=float(pos.sales_error_percent.median()) if len(pos) else None,
                  spearman=round(a.rank().corr(e.rank()), 3),
                  zero_tier_mp_sales=list(x[x.tier == 'zero'].mp_sales_best.astype(int)),
                  high_tier_actual_vs_mp=list(zip(x[x.tier == 'high'].actual_sales.astype(int), x[x.tier == 'high'].mp_sales_best.astype(int)))))
pd.DataFrame(m).to_csv(P + 'derived/calibration_metrics_sample.csv', index=False)
for i in m: print(i)
print(df[df.marketplace == 'wb'][['account', 'marketplace_id', 'tier', 'pack_qty', 'actual_sales', 'mpstats_sales_by_period_sum', 'mpstats_sales_list', 'actual_revenue', 'mpstats_revenue', 'actual_avg_price', 'mpstats_avg_price', 'mpstats_stock_days', 'notes']].to_string(max_colwidth=40))

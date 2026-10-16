# поток: mpstats-probe — калибровка оценок MPSTATS против факта (списки seller/items, 30 дней, fbs=1)
import json, pandas as pd, numpy as np, sys
sys.path.insert(0, '/opt/mp-analytics/data/mpstats_probe/2026-09-14/tmp')
P = '/opt/mp-analytics/data/mpstats_probe/2026-09-14/'
D0 = pd.Timestamp('2026-08-15')

def pack(t):
    import re
    t = str(t)
    m = re.search(r'\((\d+)\s*шт', t) or re.search(r'(\d+)\s*шт', t)
    if m: return int(m.group(1))
    if re.search(r'(Комплект|Набор|Картриджи |Фотобарабаны|Тонеры)', t): return None
    return 1

def load(files, mp):
    rows = []
    for f, acc in files:
        for r in json.load(open(P + 'raw/' + f + '.json'))['data']:
            sg = r.get('sales_graph') if mp == 'wb' else r.get('graph')
            stg = r.get('stocks_graph') or []
            rows.append(dict(account=acc, marketplace_id=str(r['id']), name=r['name'][:60], mp_sales=r['sales'] or 0,
                             mp_revenue=r['revenue'] or 0, mp_price=r.get('final_price_average') or r.get('final_price'),
                             mp_stock_days=r.get('days_in_stock'), mp_first15=sum((sg or [])[:15]), mp_last15=sum((sg or [])[15:]),
                             stock_ups=sum(max(stg[i] - stg[i-1], 0) for i in range(1, len(stg))),
                             is_fbs=r.get('is_fbs'), mp_balance=r.get('balance'), mp_rating=r.get('rating'), mp_comments=r.get('comments'),
                             mp_pics=r.get('picscount'), mp_final_price=r.get('final_price')))
    return pd.DataFrame(rows)

def spearman(a, b):
    return round(pd.Series(a).rank().corr(pd.Series(b).rank()), 3)

def metrics(df, label):
    act, mp = df.actual_orders, df.mp_sales
    pos = df[act > 0]
    ape = ((pos.mp_sales - pos.actual_orders).abs() / pos.actual_orders * 100)
    return dict(set=label, n=len(df), n_actual_pos=int((act > 0).sum()), phantom_share_pct=round((act == 0).mean() * 100, 1),
                actual_sales=int(act.sum()), mp_sales=int(mp.sum()), bias_sales_x=round(mp.sum() / max(act.sum(), 1), 1),
                wape_sales_pct=round((mp - act).abs().sum() / max(act.sum(), 1) * 100), median_ape_pct=round(ape.median()) if len(ape) else None,
                actual_rev=int(df.actual_order_sum.sum()), mp_rev=int(df.mp_revenue.sum()), bias_rev_x=round(df.mp_revenue.sum() / max(df.actual_order_sum.sum(), 1), 1),
                wape_rev_pct=round((df.mp_revenue - df.actual_order_sum).abs().sum() / max(df.actual_order_sum.sum(), 1) * 100),
                spearman_sales=spearman(mp, act), spearman_pos_only=spearman(pos.mp_sales, pos.actual_orders) if len(pos) > 2 else None)

res = []; joined = {}
for mp, files, actf, dailyf in (
        ('wb', [('s2_wb_seller_1334975_items500', 'wb_acc1'), ('s2_wb_seller_4382395_items500', 'wb_acc2')], 'actual_all_wb.csv', 'actual_daily_wb.csv'),
        ('ozon', [('s2_oz_seller_217678_items500', 'oz_acc1'), ('s2_oz_seller_2523124_items500', 'oz_acc2')], 'actual_all_oz.csv', 'actual_daily_oz.csv')):
    m = load(files, mp)
    a = pd.read_csv(P + 'derived/' + actf, dtype={'marketplace_id': str})
    a = a[['account', 'marketplace_id', 'actual_orders', 'actual_order_sum', 'actual_avg_price', 'actual_stock_days', 'fbs_mode']]
    j = m.merge(a, on=['account', 'marketplace_id'], how='left')
    j['matched'] = j.actual_orders.notna()
    j = j.fillna({'actual_orders': 0, 'actual_order_sum': 0})
    j['pack_qty'] = j.name.apply(pack)
    d = pd.read_csv(P + 'derived/' + dailyf, dtype={'nm_id': str, 'sku': str})
    idc = 'nm_id' if mp == 'wb' else 'sku'
    d['d'] = pd.to_datetime(d['od' if 'od' in d else 'd'])
    d['half'] = np.where(d.d < D0 + pd.Timedelta(days=15), 'a1', 'a2')
    h = d.pivot_table(index=['account', idc], columns='half', values='orders', aggfunc='sum').reset_index().rename(columns={idc: 'marketplace_id'})
    j = j.merge(h, on=['account', 'marketplace_id'], how='left').fillna({'a1': 0, 'a2': 0})
    joined[mp] = j
    res.append(metrics(j, f'{mp}: top-500 MPSTATS по выручке (оба кабинета)'))
    for acc in j.account.unique(): res.append(metrics(j[j.account == acc], f'{mp}: {acc}'))
    res.append(metrics(j[j.pack_qty == 1], f'{mp}: одиночные'))
    res.append(metrics(j[j.pack_qty != 1], f'{mp}: наборы/неясно'))
    # направление: только строки с продажами в обеих оценках
    dd = j[(j.a1 + j.a2 > 0) & (j.mp_first15 + j.mp_last15 > 0)]
    same = (np.sign(dd.a2 - dd.a1) == np.sign(dd.mp_last15 - dd.mp_first15)).mean() if len(dd) else None
    res[-1]['direction_ok_pct_all_rows'] = None
    res.append(dict(set=f'{mp}: направление (2-я половина vs 1-я)', n=len(dd), direction_ok_pct=round(same * 100, 1) if same is not None else None))
    # фантомы и колебания остатка
    ph = j[j.actual_orders == 0]
    res.append(dict(set=f'{mp}: фантомы — корреляция mp_sales с ростом остатка', n=len(j),
                    spearman_mp_sales_vs_stock_ups=spearman(j.mp_sales, j.stock_ups),
                    phantom_median_stock_ups=float(ph.stock_ups.median()) if len(ph) else None,
                    real_median_stock_ups=float(j[j.actual_orders > 0].stock_ups.median())))
    j.to_csv(P + f'derived/calib_list_{mp}.csv', index=False)

pd.DataFrame(res).to_csv(P + 'derived/calib_list_metrics.csv', index=False)
for r in res: print({k: v for k, v in r.items() if v is not None})

# Дни без остатка по окнам + сборка report.md из CSV. Read-only.
import pandas as pd, numpy as np, os
D = os.path.dirname(os.path.abspath(__file__)); C = D + '/cache'; T = pd.Timestamp
P = pd.read_pickle(f'{C}/post_lines.pkl'); P['day'] = pd.to_datetime(P.day)
OK = P[P.status != 'cancelled']
ST = pd.read_pickle(f'{C}/stock.pkl'); ST['day'] = pd.to_datetime(ST.day)
CODES = set(ST.external_code.dropna()); SD = ST.groupby('external_code').day.apply(set).to_dict(); SNAP = sorted(set(ST.day))
act = OK.groupby(['account', 'offer_id']).size().reset_index()[['account', 'offer_id']]
def key(o): return o if o in CODES else (o[:4] if len(o) > 4 and o[:4] in CODES else None)
act['k'] = act.offer_id.map(key)
cov = act.k.notna().mean()
def oos(a, b, acc):
    days = [d for d in SNAP if a <= d <= b]
    x = act[act.k.notna()] if acc == 'sum' else act[act.k.notna() & (act.account == acc)]
    if not days: return np.nan, np.nan, 0
    tot = len(days) * len(x); miss = sum(sum(1 for d in days if d not in SD.get(k, ())) for k in x.k)
    return miss / tot, miss / len(x), len(days)
rows = []
L = T('2026-09-13')
for acc in ['oz_acc1', 'oz_acc2', 'sum']:
    for N in (7, 14, 28, 56):
        for lab, sh in [('cur', 0), ('prev', N), ('minus4w', 28), ('minus8w', 56)]:
            b = L - pd.Timedelta(days=sh); a = b - pd.Timedelta(days=N - 1)
            share, per_sku, nd = oos(a, b, acc)
            rows.append(dict(account=acc, window=f'{N}d_{lab}', date_from=a.date(), date_to=b.date(), snap_days=nd, full=(nd == N),
                             oos_share_sku_days=share, oos_days_per_sku=per_sku))
    w0 = T('2026-06-22')
    while w0 + pd.Timedelta(days=6) <= L:
        share, per_sku, nd = oos(w0, w0 + pd.Timedelta(days=6), acc)
        rows.append(dict(account=acc, window=f'week_{w0:%m-%d}', date_from=w0.date(), date_to=(w0 + pd.Timedelta(days=6)).date(), snap_days=nd, full=(nd == 7), oos_share_sku_days=share, oos_days_per_sku=per_sku))
        w0 += pd.Timedelta(days=7)
so = pd.DataFrame(rows); so['active_sku_with_stock_key_share'] = cov
so.to_csv(f'{D}/05_stockout_windows.csv', index=False)
print('stock key coverage of active SKUs', round(cov, 3))
print(so[(so.account == 'sum') & so.window.str.startswith('28d')][['window', 'oos_share_sku_days']].round(3).values.tolist())
print(so[(so.account == 'sum') & so.window.str.startswith('week')][['window', 'oos_share_sku_days']].round(3).values.tolist())

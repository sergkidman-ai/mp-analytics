# Метрики падения Ozon: месяцы, MTD, окна, недельный тренд. Read-only (кэш из s1_extract).
import pandas as pd, numpy as np, datetime as dt, os
D = os.path.dirname(os.path.abspath(__file__)); C = D + '/cache'
pd.set_option('display.width', 250)
P = pd.read_pickle(f'{C}/post_lines.pkl'); P['day'] = pd.to_datetime(P.day)
P['price'] = P.price.astype(float); P['qty'] = P.qty.astype(float); P['rev'] = P.price * P.qty
T = pd.read_pickle(f'{C}/tx_daily.pkl'); T['day'] = pd.to_datetime(T.day)
for c in ['accr_pos', 'accr_neg', 'saldo', 'saldo_noitems']: T[c] = T[c].astype(float).fillna(0)
AD = pd.read_pickle(f'{C}/ad_daily.pkl'); AD['day'] = pd.to_datetime(AD.day); AD['spend'] = AD.spend.astype(float)
AS = pd.read_pickle(f'{C}/ads_sku.pkl'); AS['day'] = pd.to_datetime(AS.day)
for c in ['views', 'clicks', 'spend', 'orders', 'orders_money']: AS[c] = AS[c].astype(float)
SR = pd.read_pickle(f'{C}/search.pkl'); SR['period_start'] = pd.to_datetime(SR.period_start)
R = pd.read_pickle(f'{C}/returns.pkl'); R['day'] = pd.to_datetime(R.day)
AM = pd.read_pickle(f'{C}/ads_month.pkl')
ST = pd.read_pickle(f'{C}/stock.pkl'); ST['day'] = pd.to_datetime(ST.day)
M = pd.read_pickle(f'{C}/margin.pkl'); M['period_from'] = pd.to_datetime(M.period_from)
TS = pd.read_pickle(f'{C}/tx_sku_daily.pkl'); TS['day'] = pd.to_datetime(TS.day)
for c in ['accr_pos', 'accr_neg', 'saldo']: TS[c] = TS[c].astype(float)

ACC = ['oz_acc1', 'oz_acc2']
def addsum(df, keys):
    s = df.groupby([k for k in keys if k != 'account']).sum(numeric_only=True).reset_index(); s['account'] = 'sum'
    return pd.concat([df, s], ignore_index=True)

LP = pd.Timestamp('2026-09-13'); LT = pd.Timestamp('2026-09-07')
P_START = pd.Timestamp('2026-05-01'); AD_START = pd.Timestamp('2026-06-01'); AS_START = pd.Timestamp('2026-07-27')

def post_agg(df):
    ok = df[df.status != 'cancelled']; cn = df[df.status == 'cancelled']
    o = ok.posting_number.nunique(); u = ok.qty.sum(); r = ok.rev.sum()
    return dict(orders=o, units=u, rev_orders=r, avg_check=r / o if o else np.nan, avg_unit_price=r / u if u else np.nan,
                sku_sold=ok.offer_id.nunique(), cancel_posts=cn.posting_number.nunique(), cancel_rev=cn.rev.sum(),
                cancel_share=cn.posting_number.nunique() / max(1, df.posting_number.nunique()))

def period_row(acc, a, b, days_label):
    """все метрики для аккаунта за [a,b] включительно; что не покрыто данными — NaN"""
    ndays = (b - a).days + 1
    fp = P if acc == 'sum' else P[P.account == acc]
    ft = T if acc == 'sum' else T[T.account == acc]
    fa = AD if acc == 'sum' else AD[AD.account == acc]
    fs = AS if acc == 'sum' else AS[AS.account == acc]
    fr = R if acc == 'sum' else R[R.account == acc]
    row = dict(account=acc, period=days_label, date_from=a.date(), date_to=b.date(), days=ndays)
    if a >= P_START and b <= LP:
        row.update(post_agg(fp[(fp.day >= a) & (fp.day <= b)]))
    if b <= LT:
        x = ft[(ft.day >= a) & (ft.day <= b)]
        row.update(tx_revenue=x.accr_pos.sum(), tx_returns=x.accr_neg.sum(), tx_saldo=x.saldo.sum(), tx_overhead_noitems=x.saldo_noitems.sum())
    if a >= AD_START and b <= LP:
        row['ad_spend_all'] = fa[(fa.day >= a) & (fa.day <= b)].spend.sum()
        if 'rev_orders' in row: row['drr_all_vs_orders'] = row['ad_spend_all'] / row['rev_orders'] if row['rev_orders'] else np.nan
    if a >= AS_START and b <= LP:
        x = fs[(fs.day >= a) & (fs.day <= b)]
        v, cl, sp = x.views.sum(), x.clicks.sum(), x.spend.sum()
        row.update(skuads_spend=sp, skuads_views=v, skuads_clicks=cl, skuads_ctr=cl / v if v else np.nan, skuads_cpc=sp / cl if cl else np.nan,
                   skuads_orders=x.orders.sum(), skuads_orders_money=x.orders_money.sum(),
                   skuads_drr=sp / x.orders_money.sum() if x.orders_money.sum() else np.nan, skuads_sku_with_views=x[x.views > 0].sku.nunique())
    x = fr[(fr.day >= a) & (fr.day <= b)]
    row.update(returns_cnt=x.return_id.nunique(), returns_amount=x.drop_duplicates('return_id').amount.astype(float).sum())
    return row

rows = []
# месяцы
for acc in ACC + ['sum']:
    for m in range(3, 9):
        a = pd.Timestamp(2026, m, 1); b = a + pd.offsets.MonthEnd(0)
        rows.append(period_row(acc, a, b, f'month_{a:%Y-%m}'))
    for lab, a, b in [('tx_MTD_09-01..07', '2026-09-01', '2026-09-07'), ('tx_08-01..07', '2026-08-01', '2026-08-07'), ('tx_07-01..07', '2026-07-01', '2026-07-07'),
                      ('post_MTD_09-01..13', '2026-09-01', '2026-09-13'), ('post_08-01..13', '2026-08-01', '2026-08-13'), ('post_07-01..13', '2026-07-01', '2026-07-13')]:
        rows.append(period_row(acc, pd.Timestamp(a), pd.Timestamp(b), lab))
mon = pd.DataFrame(rows); mon.to_csv(f'{D}/01_months_mtd.csv', index=False)

# окна
rows = []
for acc in ACC + ['sum']:
    for src, L in [('postings', LP), ('tx', LT)]:
        for N in (7, 14, 28, 56):
            for lab, shift in [('cur', 0), ('prev', N), ('minus4w', 28), ('minus8w', 56)]:
                b = L - pd.Timedelta(days=shift); a = b - pd.Timedelta(days=N - 1)
                r = period_row(acc, a, b, f'{src}_{N}d_{lab}'); r['src'] = src
                rows.append(r)
win = pd.DataFrame(rows)
# оставить только релевантные источнику колонки
win.to_csv(f'{D}/02_windows.csv', index=False)

# недельный тренд (пн–вс), только полные недели
wk = []
w0 = pd.Timestamp('2026-05-04')
while w0 + pd.Timedelta(days=6) <= LP:
    w1 = w0 + pd.Timedelta(days=6)
    for acc in ACC + ['sum']:
        r = period_row(acc, w0, w1, f'week_{w0:%m-%d}')
        fsr = SR if acc == 'sum' else SR[SR.account == acc]
        x = fsr[fsr.period_start == w0]
        r['search_views'] = x.unique_view_users.sum() if len(x) else np.nan
        r['search_users'] = x.unique_search_users.sum() if len(x) else np.nan
        r['search_gmv'] = x.gmv.astype(float).sum() if len(x) else np.nan
        r['search_sku'] = x[x.unique_view_users > 0].sku.nunique() if len(x) else np.nan
        wk.append(r)
    w0 += pd.Timedelta(days=7)
wk = pd.DataFrame(wk); wk.to_csv(f'{D}/03_weekly.csv', index=False)

# оценка contribution по месяцам: сальдо транзакций с items − себест margin_by_sku (оценка, не прибыль BI)
TS['m'] = TS.day.dt.to_period('M').dt.to_timestamp()
cs = TS.groupby(['account', 'm']).saldo.sum().rename('saldo_items').reset_index()
cg = M.groupby(['account', 'period_from']).agg(cogs=('cogs', lambda s: s.astype(float).sum())).reset_index().rename(columns={'period_from': 'm'})
ov = T.assign(m=T.day.dt.to_period('M').dt.to_timestamp()).groupby(['account', 'm']).agg(revenue=('accr_pos', 'sum'), overhead_noitems=('saldo_noitems', 'sum')).reset_index()
co = ov.merge(cs, on=['account', 'm'], how='left').merge(cg, on=['account', 'm'], how='left')
co['contribution_est'] = co.saldo_items - co.cogs
co['contribution_after_overhead_est'] = co.contribution_est + co.overhead_noitems
co['contrib_pct'] = co.contribution_est / co.revenue
co = addsum(co, ['account', 'm']); co['contrib_pct'] = co.contribution_est / co.revenue
co.to_csv(f'{D}/04_contribution_est_monthly.csv', index=False)

# сводка в консоль
def show(df, cols):
    print(df[cols].round(3).to_string(index=False))
s = mon[mon.account == 'sum']
show(s, ['period', 'tx_revenue', 'rev_orders', 'orders', 'units', 'avg_check', 'avg_unit_price', 'sku_sold', 'cancel_share', 'ad_spend_all'])
w = wk[wk.account == 'sum']
show(w, ['period', 'rev_orders', 'orders', 'units', 'avg_unit_price', 'sku_sold', 'cancel_share', 'tx_revenue', 'ad_spend_all', 'skuads_views', 'search_views'])

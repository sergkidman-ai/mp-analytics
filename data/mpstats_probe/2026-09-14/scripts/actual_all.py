# поток: mpstats-probe — факт по всем SKU за 2026-08-15..09-13 (read-only)
import re, random, csv, datetime as dt
import pandas as pd
from core.db import get_conn

OUT = '/opt/mp-analytics/data/mpstats_probe/2026-09-14/derived/'
D0, D1 = '2026-08-15', '2026-09-13'
EXTRA_WB, EXTRA_OZ = 181081324, '1822591480'
random.seed(14)

def q(sql, params=None):
    with get_conn() as c:
        return pd.read_sql(sql, c, params=params)

# ---------------- pack parsing
def pack(title):
    t = (title or '')
    tl = t.lower()
    m = re.search(r'(\d+)\s*шт', tl) or re.search(r'(?:комплект|набор)\s+из\s+(\d+)', tl) \
        or re.search(r'(\d+)\s*(?:цвет|картридж)', tl)
    is_set = bool(re.search(r'комплект|набор|\d+\s*шт|картриджи\b', tl))
    if m and 1 <= int(m.group(1)) <= 20:
        n = int(m.group(1)); return n, n > 1, 'title_count'
    # code range like T6931-T6935 / CF360A-CF363A
    r = re.search(r'\b([A-Z]{1,4})(\d{2,5})([A-Z]{0,2})\s*-\s*\1?(\d{2,5})\3\b', t)
    if r:
        a, b = int(r.group(2)), int(r.group(4))
        if 1 < b - a + 1 <= 8:
            return b - a + 1, True, 'code_range(uncertain)'
    if is_set:
        return None, True, 'set_no_count(uncertain)'
    return 1, False, 'single'

# ---------------- WB
wb_rep = q("""
with r as (
  select distinct on (rrd_id) account, (payload->>'nm_id')::bigint nm_id, payload->>'srid' srid,
    (payload->>'order_dt')::date od, (payload->>'sale_dt')::date sd, payload->>'supplier_oper_name' op,
    payload->>'delivery_method' dm, (payload->>'retail_price_withdisc_rub')::numeric pwd,
    (payload->>'retail_amount')::numeric ra, (payload->>'quantity')::numeric qty
  from raw_wb_report where (payload->>'order_dt')::date >= '2026-08-01' or (payload->>'sale_dt')::date >= '2026-08-01'
)
select * from r where nm_id > 0""")
ordrows = wb_rep[wb_rep.op.isin(['Продажа', 'Логистика', 'Доставка', 'Возврат'])]
first = ordrows.sort_values('op', key=lambda s: s.ne('Продажа')).drop_duplicates(['account', 'srid'])
first = first[(first.od >= pd.Timestamp(D0).date()) & (first.od <= pd.Timestamp(D1).date())]
wb_daily_orders = first.groupby(['account', 'nm_id', 'od']).srid.nunique().rename('orders').reset_index()
aug2 = first[first.od <= dt.date(2026, 8, 31)]
# order sum for report part: price after seller discount of the sale row (if any) else max pwd on srid
pw = ordrows.groupby(['account', 'srid']).pwd.max().rename('pwd_srid')
aug2 = aug2.join(pw, on=['account', 'srid'])
rep_aug = aug2.groupby(['account', 'nm_id']).agg(rep_orders_0815_0831=('srid', 'nunique'), rep_ordsum_0815_0831=('pwd_srid', 'sum')).reset_index()
rep_win = first.groupby(['account', 'nm_id']).agg(rep_orders_win_orderdt=('srid', 'nunique')).reset_index()
sales = wb_rep[(wb_rep.sd >= dt.date(2026, 8, 15)) & (wb_rep.sd <= dt.date(2026, 9, 13))]
s = sales[sales.op == 'Продажа'].groupby(['account', 'nm_id']).agg(bo=('qty', 'sum'), bo_pwd=('pwd', 'sum'), bo_ra=('ra', 'sum')).reset_index()
rt = sales[sales.op == 'Возврат'].groupby(['account', 'nm_id']).agg(ret=('qty', 'sum'), ret_pwd=('pwd', 'sum')).reset_index()
dm = ordrows[ordrows.dm.notna() & (ordrows.dm != '')].copy()
dm['m'] = dm.dm.str.extract(r'^(FBS|FBW|FBO|DBS|DBW|EDBS|C&C)')[0]
dmode = dm.groupby(['account', 'nm_id']).m.agg(lambda x: '/'.join(sorted(set(x.dropna())))).rename('rep_modes').reset_index()

fun = q("""select account, nm_id, period, order_count, order_sum, buyout_count, buyout_sum, stock_wb, stock_mp,
           product_rating, feedback_rating from wb_funnel where period in ('2026-08-01','2026-09-01')""")
fa = fun[fun.period == dt.date(2026, 8, 1)][['account', 'nm_id', 'order_count', 'order_sum']].rename(columns={'order_count': 'fun_aug_orders', 'order_sum': 'fun_aug_ordsum'})
fs = fun[fun.period == dt.date(2026, 9, 1)][['account', 'nm_id', 'order_count', 'order_sum', 'buyout_count', 'stock_wb', 'stock_mp']].rename(
    columns={'order_count': 'fun_sep_orders', 'order_sum': 'fun_sep_ordsum', 'buyout_count': 'fun_sep_buyouts', 'stock_wb': 'fun_stock_wb_now', 'stock_mp': 'fun_stock_mp_now'})

cards = q("select account, nm_id, vendor_code, title, in_cabinet, in_trash from wb_cards_live")
wbst = q("""select account, nm_id, captured_at d, sum(quantity) fbo from wb_stocks
            where captured_at between %s and %s and warehouse <> '(в пути)' group by 1,2,3""", (D0, D1))
ms = q("""select external_code, captured_at d, sum(greatest(coalesce(stock,0)-coalesce(reserve,0),0)) ms
          from supplier_stock where captured_at between %s and %s and store not in ('Брак','Транзит')
          group by 1,2""", (D0, D1))

w = cards.copy()
for df in [rep_aug, rep_win, s, rt, dmode, fa, fs]:
    w = w.merge(df, on=['account', 'nm_id'], how='left')
w = w.fillna({c: 0 for c in ['rep_orders_0815_0831', 'rep_ordsum_0815_0831', 'rep_orders_win_orderdt', 'bo', 'bo_pwd', 'bo_ra', 'ret', 'ret_pwd', 'fun_sep_orders', 'fun_sep_ordsum', 'fun_aug_orders']})
w['actual_orders'] = w.rep_orders_0815_0831 + w.fun_sep_orders
w['actual_order_sum'] = (w.rep_ordsum_0815_0831 + w.fun_sep_ordsum).round(0)
w['actual_buyouts'] = w.bo - w.ret
w['actual_buyout_sum'] = (w.bo_pwd - w.ret_pwd).round(0)
w['actual_avg_price'] = (w.actual_order_sum / w.actual_orders.where(w.actual_orders > 0)).round(0)
w['avg_buyer_price_retail_amount'] = (w.bo_ra / w.bo.where(w.bo > 0)).round(0)
fbo_days = wbst[wbst.fbo > 0].groupby(['account', 'nm_id']).d.nunique().rename('fbo_stock_days').reset_index()
w = w.merge(fbo_days, on=['account', 'nm_id'], how='left')
msd = ms[ms.ms > 0].groupby('external_code').d.nunique().rename('ms_stock_days')
w = w.merge(msd, left_on='vendor_code', right_index=True, how='left').fillna({'fbo_stock_days': 0, 'ms_stock_days': 0})
w['actual_stock_days'] = w[['fbo_stock_days', 'ms_stock_days']].max(axis=1)
def fmode(r):
    m = r.rep_modes if isinstance(r.rep_modes, str) and r.rep_modes else ''
    if not m:
        m = 'FBW(stock)' if r.fbo_stock_days > 0 else 'FBS(assumed)'
    return m
w['fbs_mode'] = w.apply(fmode, axis=1)
w['marketplace'] = 'wb'; w['marketplace_id'] = w.nm_id.astype(str); w['sku'] = w.vendor_code
w['active'] = w.in_cabinet.fillna(False) & ~w.in_trash.fillna(False)

# ---------------- OZON
post = q("""
select distinct on (account, posting_number) account, posting_number, scheme, status, in_process_at::date d, payload
from raw_ozon_posting where in_process_at::date between %s and %s order by account, posting_number, loaded_at desc""", (D0, D1))
rows = []
for r in post.itertuples():
    for p in (r.payload or {}).get('products', []):
        rows.append((r.account, str(p['sku']), p.get('offer_id'), r.scheme, r.status, r.d, float(p.get('quantity') or 0), float(p.get('price') or 0)))
op = pd.DataFrame(rows, columns=['account', 'sku', 'offer_id', 'scheme', 'status', 'd', 'qty', 'price'])
op['ok'] = op.status != 'cancelled'
op['rev'] = op.qty * op.price
ok = op[op.ok]
oz_daily_orders = ok.groupby(['account', 'sku', 'd']).qty.sum().rename('orders').reset_index()
oagg = ok.groupby(['account', 'sku']).agg(actual_orders=('qty', 'sum'), actual_order_sum=('rev', 'sum')).reset_index()
oall = op.groupby(['account', 'sku']).agg(orders_incl_cancel=('qty', 'sum'), modes=('scheme', lambda x: '/'.join(sorted(set(x))).upper())).reset_index()
dl = op[op.status == 'delivered'].groupby(['account', 'sku']).agg(actual_buyouts=('qty', 'sum'), actual_buyout_sum=('rev', 'sum')).reset_index()
prod = q("select account, sku, offer_id, name title, is_archived from ozon_product")
ofbo = q("""select account, sku, captured_at d, sum(free_to_sell) fbo from ozon_fbo_stock
            where captured_at between %s and %s group by 1,2,3""", (D0, D1))
o = prod.merge(oagg, on=['account', 'sku'], how='left').merge(oall, on=['account', 'sku'], how='left').merge(dl, on=['account', 'sku'], how='left')
o = o.fillna({'actual_orders': 0, 'actual_order_sum': 0, 'orders_incl_cancel': 0, 'actual_buyouts': 0, 'actual_buyout_sum': 0})
o['actual_avg_price'] = (o.actual_order_sum / o.actual_orders.where(o.actual_orders > 0)).round(0)
ofd = ofbo[ofbo.fbo > 0].groupby(['account', 'sku']).d.nunique().rename('fbo_stock_days').reset_index()
o = o.merge(ofd, on=['account', 'sku'], how='left').merge(msd, left_on='offer_id', right_index=True, how='left').fillna({'fbo_stock_days': 0, 'ms_stock_days': 0})
o['actual_stock_days'] = o[['fbo_stock_days', 'ms_stock_days']].max(axis=1)
o['fbs_mode'] = o.apply(lambda r: r.modes if isinstance(r.modes, str) else ('FBO(stock)' if r.fbo_stock_days > 0 else 'FBS(assumed)'), axis=1)
o['marketplace'] = 'ozon'; o['marketplace_id'] = o.sku; o['sku_code'] = o.offer_id
o['active'] = ~o.is_archived.fillna(False)
w.drop(columns=["in_cabinet","in_trash"]).to_csv(OUT+"actual_all_wb.csv", index=False)
o.to_csv(OUT+"actual_all_oz.csv", index=False)
wb_daily_orders.to_csv(OUT+"actual_daily_wb.csv", index=False); oz_daily_orders.to_csv(OUT+"actual_daily_oz.csv", index=False)
import sys; sys.exit(0)

# ---------------- sampling
def tiers(df, key='actual_orders'):
    df = df.copy()
    pk = df.title.apply(pack)
    df['pack_qty'] = [p[0] for p in pk]; df['is_set'] = [p[1] for p in pk]; df['pack_src'] = [p[2] for p in pk]
    pos = df[df[key] >= 4].sort_values(key, ascending=False)
    ntop = max(15, int(len(pos) * 0.1))
    high = pos.head(ntop); med = pos.iloc[ntop:]
    med = med[(med[key] >= med[key].quantile(0.25)) & (med[key] <= med[key].quantile(0.9))]
    low = df[(df[key] >= 1) & (df[key] <= 3)]
    zero = df[(df[key] == 0) & df.active & (df.actual_stock_days > 0) & (df.get('orders_incl_cancel', df[key]) == 0)]
    picks = []
    for name, pool in [('high', high), ('medium', med), ('low', low), ('zero', zero)]:
        pool = pool[~pool.marketplace_id.isin([str(EXTRA_WB), EXTRA_OZ])]
        chosen = []
        accs = sorted(pool.account.unique())
        plan = [accs[0], accs[-1], accs[0], accs[-1], accs[0]] if accs else []
        sets_needed = 2
        for i, acc in enumerate(plan):
            p = pool[(pool.account == acc) & ~pool.index.isin(chosen)]
            if sets_needed > 0 and p.is_set.any():
                p = p[p.is_set]; sets_needed -= 1
            elif (~p.is_set).any() and i < 3:
                p = p[~p.is_set]
            if len(p):
                chosen.append(random.choice(list(p.index)))
        t = df.loc[chosen].copy(); t['sales_tier'] = name; picks.append(t)
    ex = df[df.marketplace_id.isin([str(EXTRA_WB), EXTRA_OZ])].copy(); ex['sales_tier'] = 'extra'
    return pd.concat(picks + [ex])

cols = ['marketplace', 'account', 'sku', 'marketplace_id', 'title', 'pack_qty', 'pack_src', 'is_set', 'actual_orders', 'actual_order_sum',
        'actual_buyouts', 'actual_buyout_sum', 'actual_avg_price', 'actual_stock_days', 'fbo_stock_days', 'ms_stock_days', 'fbs_mode', 'sales_tier', 'daily_series']
ws = tiers(w)
ws['daily_series'] = 'calib_daily_actual.csv'
os_ = tiers(o); os_['sku'] = os_.offer_id; os_['daily_series'] = 'calib_daily_actual.csv'
wbextra = ['rep_orders_0815_0831', 'fun_sep_orders', 'rep_orders_win_orderdt', 'fun_aug_orders', 'avg_buyer_price_retail_amount', 'fun_stock_mp_now']
samp = pd.concat([ws[cols + wbextra], os_[cols + ['orders_incl_cancel']]])
samp['title'] = samp.title.str.slice(0, 60)
samp.to_csv(OUT + 'calib_sample_actual.csv', index=False)

# daily series
days = pd.date_range(D0, D1).date
dl_rows = []
for r in samp.itertuples():
    if r.marketplace == 'wb':
        od = wb_daily_orders[(wb_daily_orders.account == r.account) & (wb_daily_orders.nm_id == int(r.marketplace_id))].set_index('od').orders
        fb = wbst[(wbst.account == r.account) & (wbst.nm_id == int(r.marketplace_id))].set_index('d').fbo
    else:
        od = oz_daily_orders[(oz_daily_orders.account == r.account) & (oz_daily_orders.sku == r.marketplace_id)].set_index('d').orders
        fb = ofbo[(ofbo.account == r.account) & (ofbo.sku == r.marketplace_id)].set_index('d').fbo
    mss = ms[ms.external_code == r.sku].set_index('d').ms
    for d in days:
        dl_rows.append((d, r.marketplace, r.account, r.marketplace_id, r.sales_tier, int(od.get(d, 0)),
                        fb.get(d, 0), mss.get(d, None)))
pd.DataFrame(dl_rows, columns=['date', 'marketplace', 'account', 'marketplace_id', 'sales_tier', 'orders', 'stock_fbo', 'stock_ms_avail']).to_csv(OUT + 'calib_daily_actual.csv', index=False)

# seller totals
tot = []
for acc, g in w.groupby('account'):
    tot.append(dict(marketplace='wb', account=acc, seller_id={'wb_acc1': 1334975, 'wb_acc2': 4382395}[acc],
                    org={'wb_acc1': 'ООО Цифровой Квадрат', 'wb_acc2': 'ООО Дисквэр'}[acc], inn={'wb_acc1': '7807355364', 'wb_acc2': '7811803918'}[acc],
                    orders_hybrid=int(g.actual_orders.sum()), order_sum_hybrid=round(g.actual_order_sum.sum()),
                    orders_report_orderdt_win=int(g.rep_orders_win_orderdt.sum()), fun_aug_orders_fullmonth=int(g.fun_aug_orders.sum()),
                    fun_sep_orders_1_14=int(g.fun_sep_orders.sum()),
                    buyouts_net_saledt=int(g.actual_buyouts.sum()), buyout_sum_net=round(g.actual_buyout_sum.sum()),
                    sku_with_orders=int((g.actual_orders > 0).sum()), active_cards=int(g.active.sum()), cards_total=len(g),
                    sku_with_stock_in_window=int((g.actual_stock_days > 0).sum())))
for acc, g in o.groupby('account'):
    tot.append(dict(marketplace='ozon', account=acc, seller_id='', org={'oz_acc1': 'Премиум-Про (Цифровой Квадрат?)', 'oz_acc2': 'ООО Дисквэр'}[acc],
                    inn={'oz_acc1': '7807355364?', 'oz_acc2': '7811803918'}[acc],
                    orders_hybrid=int(g.actual_orders.sum()), order_sum_hybrid=round(g.actual_order_sum.sum()),
                    orders_incl_cancel=int(g.orders_incl_cancel.sum()),
                    buyouts_net_saledt=int(g.actual_buyouts.sum()), buyout_sum_net=round(g.actual_buyout_sum.sum()),
                    sku_with_orders=int((g.actual_orders > 0).sum()), active_cards=int(g.active.sum()), cards_total=len(g),
                    sku_with_stock_in_window=int((g.actual_stock_days > 0).sum())))
pd.DataFrame(tot).to_csv(OUT + 'seller_totals.csv', index=False)

# search queries
wsel = ws[(ws.sales_tier != 'zero')].marketplace_id.astype(int).tolist()
st = q("""select nm_id, text query, sum(orders) orders, sum(open_card) open_card, sum(frequency) freq, min(period_start) p0, max(period_start) p1
          from wb_search_text where period_start >= '2026-08-04' group by 1,2""")
st = st[st.nm_id.isin(w[w.actual_orders > 0].nm_id)]
have = [n for n in wsel if n in set(st.nm_id)]
others = st.groupby('nm_id').orders.sum().sort_values(ascending=False).index.tolist()
nms = (have + [n for n in others if n not in have])[:10]
sq = []
for n in nms:
    g = st[st.nm_id == n].sort_values(['orders', 'open_card'], ascending=False).head(10)
    for r in g.itertuples():
        sq.append(('wb', n, r.query, f'orders={r.orders};open_card={r.open_card};freq={r.freq}', f'{r.p0}..{r.p1}(daily sums)', n in have))
oq = q("""select sku, query, sum(order_count) orders, sum(unique_view_users) views, sum(unique_search_users) searches from ozon_search_query
          where period_start='2026-08-31' and period_end='2026-09-07' group by 1,2""")
osel = os_[os_.sales_tier != 'zero'].marketplace_id.tolist()
have_o = [s_ for s_ in osel if s_ in set(oq.sku)]
oth = oq[oq.sku.isin(o[o.actual_orders > 0].sku)].groupby('sku').orders.sum().sort_values(ascending=False).index.tolist()
skus = (have_o + [s_ for s_ in oth if s_ not in have_o])[:10]
for s_ in skus:
    g = oq[oq.sku == s_].sort_values(['orders', 'views'], ascending=False).head(10)
    for r in g.itertuples():
        sq.append(('ozon', s_, r.query, f'orders={r.orders};views={r.views};searches={r.searches}', '2026-08-31..2026-09-07', s_ in have_o))
pd.DataFrame(sq, columns=['marketplace', 'marketplace_id', 'query', 'metric', 'period', 'in_sample']).to_csv(OUT + 'our_search_queries.csv', index=False)

# Q2612A
qa = []
wp = q("select * from wb_price where nm_id=%s order by captured_at desc limit 1", (EXTRA_WB,))
wr = w[w.nm_id == EXTRA_WB]
fr = fun[(fun.nm_id == EXTRA_WB)].sort_values('period').tail(1)
for r in wr.itertuples():
    pq = pack(r.title)
    qa.append(dict(marketplace='wb', account=r.account, marketplace_id=r.nm_id, sku=r.vendor_code, title=r.title, pack_qty=pq[0], pack_src=pq[2],
                   price=wp.price.iloc[0] if len(wp) else None, discounted_price=wp.discounted_price.iloc[0] if len(wp) else None,
                   club_price=wp.club_price.iloc[0] if len(wp) else None, market_price=wp.market_price.iloc[0] if len(wp) else None,
                   price_captured=wp.captured_at.iloc[0] if len(wp) else None,
                   rating=fr.product_rating.iloc[0] if len(fr) else None, feedback_rating=fr.feedback_rating.iloc[0] if len(fr) else None,
                   orders_30d=r.actual_orders, buyouts_30d=r.actual_buyouts, stock_days=r.actual_stock_days, fbs_mode=r.fbs_mode,
                   stock_mp_now=r.fun_stock_mp_now, active=r.active))
op_ = q("select * from ozon_price_index where sku=%s order by collected_on desc limit 1", (EXTRA_OZ,))
orr = q("select * from ozon_rating where sku=%s", (EXTRA_OZ,))
for r in o[o.sku == EXTRA_OZ].itertuples():
    pq = pack(r.title)
    qa.append(dict(marketplace='ozon', account=r.account, marketplace_id=r.sku, sku=r.offer_id, title=r.title, pack_qty=pq[0], pack_src=pq[2],
                   price=op_.old_price.iloc[0] if len(op_) else None, discounted_price=op_.price.iloc[0] if len(op_) else None,
                   club_price=op_.marketing_price.iloc[0] if len(op_) else None, market_price=op_.ozon_min_price.iloc[0] if len(op_) else None,
                   price_captured=op_.collected_on.iloc[0] if len(op_) else None,
                   rating=orr.avg_rating.iloc[0] if len(orr) else None, feedback_rating=orr.reviews_count.iloc[0] if len(orr) else None,
                   orders_30d=r.actual_orders, buyouts_30d=r.actual_buyouts, stock_days=r.actual_stock_days, fbs_mode=r.fbs_mode, active=r.active))
# other Q2612A cards of ours
for r in w[w.title.fillna('').str.contains('Q2612|2612A', case=False) & (w.nm_id != EXTRA_WB)].sort_values('actual_orders', ascending=False).head(8).itertuples():
    qa.append(dict(marketplace='wb', account=r.account, marketplace_id=r.nm_id, sku=r.vendor_code, title=r.title, orders_30d=r.actual_orders, stock_days=r.actual_stock_days, active=r.active, note='other Q2612A card'))
for r in o[o.title.fillna('').str.contains('Q2612|2612A', case=False) & (o.sku != EXTRA_OZ)].sort_values('actual_orders', ascending=False).head(8).itertuples():
    qa.append(dict(marketplace='ozon', account=r.account, marketplace_id=r.sku, sku=r.offer_id, title=r.title, orders_30d=r.actual_orders, stock_days=r.actual_stock_days, active=r.active, note='other Q2612A card'))
pd.DataFrame(qa).to_csv(OUT + 'q2612a_ours.csv', index=False)

# summary
with open('/tmp/claude-0/-opt-mp-analytics/663a0611-08f4-473d-984b-c161d423bde1/scratchpad/summary.txt', 'w') as f:
    for mp, g in samp.groupby('marketplace'):
        for t, gg in g.groupby('sales_tier', sort=False):
            f.write(f"{mp} {t}: " + ','.join(f"{a[-1]}:{i}({int(x)}{'S' if s_ else ''})" for a, i, x, s_ in zip(gg.account, gg.marketplace_id, gg.actual_orders, gg.is_set)) + '\n')
    f.write(pd.DataFrame(tot).to_string() + '\n')
    f.write(f"search rows {len(sq)} wb_nm={len(nms)} (in sample {len(have)}) oz_sku={len(skus)} (in sample {len(have_o)})\n")
    f.write(f"q2612a rows {len(qa)}\n")
print('ok')

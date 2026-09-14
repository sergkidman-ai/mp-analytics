import sys; sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles')
from prep import *
O='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles/'
P0,P1=pd.Timestamp('2026-08-08'),pd.Timestamp('2026-09-13')
TX_END=pd.Timestamp('2026-09-07')
out={}

# ---------- offer-level dimension ----------
off=prod.sort_values('is_archived').drop_duplicates(['account','offer_id'])[['account','offer_id','base','n','name','is_family']]
off=off[off.is_family]
BUNDLE_OTHER=set(zip(OTHER_BUNDLE_CAMPS.account, OTHER_BUNDLE_CAMPS.sku))
sku_off=prod[['account','sku','offer_id']]
camp_offers=set(camp.offer_id)
oth_offers=set(prod.merge(OTHER_BUNDLE_CAMPS[['account','sku']], on=['account','sku']).offer_id)
def adgroup(r):
    if r.n==1: return 'single(база в 35269713)' if (r.account=='oz_acc1' and r.base in CAMP_BASES) else 'single(прочие базы)'
    if r.offer_id in camp_offers: return 'в 35269713'
    if r.offer_id in oth_offers: return 'в др. кампании'
    return 'вне рекламы'
off['grp']=off.apply(adgroup, axis=1)

# ---------- unit cost of base ----------
st=L('stock'); pc=L('products_cost'); mc=L('ms_cost')
c1=st[(st.d>=P0)&(st.d<=P1)].groupby('external_code').cost_seb_med.median()
c2=pc[pc.cost_seb>0].groupby('external_code').cost_seb.median()
c3=mc[mc.buy_price>0].groupby('external_code').buy_price.median()
def ucost(b):
    for s,src in ((c1,'supplier_stock.cost_seb'),(c2,'products.cost_seb'),(c3,'ms_product.buy_price')):
        if b in s.index and s[b]>0: return float(s[b]),src
    return np.nan,'нет'
bc=pd.DataFrame([(b,)+ucost(b) for b in off.base.unique()], columns=['base','unit_cost','cost_src'])
off=off.merge(bc,on='base',how='left')
off['set_cost']=off.unit_cost*off.n
print('cost src (bases):', bc.cost_src.value_counts().to_dict())

# ---------- facts ----------
p=L('post').merge(off[['account','offer_id']], on=['account','offer_id'])
p['cancel']=p.status.eq('cancelled')
ads=L('ads').merge(sku_off,on=['account','sku']).merge(off[['account','offer_id']],on=['account','offer_id'])
tx=L('tx').merge(sku_off,on=['account','sku']).merge(off[['account','offer_id']],on=['account','offer_id'])
for col in ('accr','comm','amount'): tx[col]=tx[col]/tx.nitems
ret=L('ret').merge(off[['account','offer_id']],on=['account','offer_id'])
srch=L('search').merge(sku_off,on=['account','sku']).merge(off[['account','offer_id']],on=['account','offer_id'])

def per_offer(d0,d1,tx_end=None):
    a=p[(p.d>=d0)&(p.d<=d1)]
    ok=a[~a.cancel].groupby(['account','offer_id']).agg(orders=('posting_number','nunique'),qty=('qty','sum'))
    ok['rev']=a[~a.cancel].assign(v=lambda x:x.price*x.qty).groupby(['account','offer_id']).v.sum()
    ok['cancels']=a[a.cancel].groupby(['account','offer_id']).posting_number.nunique()
    ok=ok.reindex(ok.index.union(a[a.cancel].groupby(['account','offer_id']).size().index))
    ok['cancels']=a[a.cancel].groupby(['account','offer_id']).posting_number.nunique()
    ad=ads[(ads.d>=d0)&(ads.d<=d1)].groupby(['account','offer_id'])[['views','clicks','money_spent','orders_qty','orders_money']].sum()
    r=ret[(ret.d>=d0)&(ret.d<=d1)].groupby(['account','offer_id']).qty.sum().rename('ret_qty')
    te=min(d1,tx_end) if tx_end is not None else d1
    t=tx[(tx.d>=d0)&(tx.d<=te)]
    tf=t.groupby(['account','offer_id']).agg(accr_pos=('accr',lambda s:s[s>0].sum()),accr_neg=('accr',lambda s:s[s<0].sum()),comm=('comm','sum'),amount=('amount','sum'))
    tf['n_sales_tx']=t[t.accr>0].groupby(['account','offer_id']).size()
    tf['n_ret_tx']=t[t.accr<0].groupby(['account','offer_id']).size()
    x=pd.concat([ok,ad,r,tf],axis=1).fillna(0).reset_index()
    return x

fullP=per_offer(P0,P1,TX_END).merge(off,on=['account','offer_id'],how='right').fillna({'orders':0})
num=['orders','qty','rev','cancels','views','clicks','money_spent','orders_qty','orders_money','ret_qty','accr_pos','accr_neg','comm','amount','n_sales_tx','n_ret_tx']
fullP[num]=fullP[num].fillna(0)

# ---------- payout rates by account x size (tx 08.08-07.09) ----------
rt=fullP.groupby(['account','n'])[['accr_pos','accr_neg','comm','amount','n_sales_tx']].sum()
bundle_pool=fullP[fullP.n>1].groupby('account')[['accr_pos','accr_neg','comm','amount','n_sales_tx']].sum()
def rates(acc,n):
    r=rt.loc[(acc,n)] if (acc,n) in rt.index else None
    src='размер'
    if r is None or r.n_sales_tx<10:
        r=bundle_pool.loc[acc] if n>1 else r; src='пул наборов акк.' if n>1 else 'размер(мало)'
    if r is None or r.accr_pos<=0: return (np.nan,np.nan,np.nan,'нет')
    return (r.amount/r.accr_pos, -r.comm/r.accr_pos, -(r.amount-r.accr_pos-r.accr_neg-r.comm)/r.accr_pos, src)
RT={k:rates(*k) for k in set(zip(fullP.account,fullP.n))}
fullP['payout_ratio']=[RT[(a,n)][0] for a,n in zip(fullP.account,fullP.n)]

def econ(x):
    x=x.copy()
    x['cart_qty']=x.qty*x.n
    x['cogs']=x.qty*x.set_cost.fillna(0)
    x['rev_net_est']=x.rev*x.payout_ratio
    x['contrib_est']=x.rev_net_est-x.cogs-x.money_spent
    return x
fullP=econ(fullP)
fullP.to_csv(O+'offers_period.csv',index=False)

def agg(g):
    s=g[num+['cart_qty','cogs','rev_net_est','contrib_est']].sum()
    s['offers']=len(g); s['offers_with_orders']=(g.orders>0).sum()
    s['nocost_rev_share']=g.loc[g.unit_cost.isna(),'rev'].sum()/max(g.rev.sum(),1)
    return s
def ratios(t):
    t['CTR_%']=100*t.clicks/t.views.replace(0,np.nan)
    t['CPC']=t.money_spent/t.clicks.replace(0,np.nan)
    t['avg_check']=t.rev/t.orders.replace(0,np.nan)
    t['CR_orders_per_click_%']=100*t.orders/t.clicks.replace(0,np.nan)
    t['CR_ad_attrib_per_click_%']=100*t.orders_qty/t.clicks.replace(0,np.nan)
    t['cancel_%']=100*t.cancels/(t.orders+t.cancels).replace(0,np.nan)
    t['return_%_qty']=100*t.ret_qty/t.qty.replace(0,np.nan)
    t['contrib_per_order']=t.contrib_est/t.orders.replace(0,np.nan)
    t['contrib_preads_per_rub_ad']=(t.contrib_est+t.money_spent)/t.money_spent.replace(0,np.nan)
    t['DRR_%']=100*t.money_spent/t.rev.replace(0,np.nan)
    return t
sz=ratios(fullP.groupby(['account','n','grp']).apply(agg).reset_index())
szn=ratios(fullP.groupby(['account','n']).apply(agg).reset_index())
szn['rate_src']=[RT[(a,n)][3] for a,n in zip(szn.account,szn.n)]
szn['payout_ratio']=[RT[(a,n)][0] for a,n in zip(szn.account,szn.n)]
szn['comm_rate']=[RT[(a,n)][1] for a,n in zip(szn.account,szn.n)]
szn['logist_other_rate']=[RT[(a,n)][2] for a,n in zip(szn.account,szn.n)]

# fact contribution to 07.09 by transactions
f=fullP.copy(); f['cogs_fact']=(f.n_sales_tx-f.n_ret_tx)*f.set_cost.fillna(0)
adsF=ads[(ads.d>=P0)&(ads.d<=TX_END)].groupby(['account','offer_id']).money_spent.sum().rename('ads_to0907')
f=f.merge(adsF,on=['account','offer_id'],how='left').fillna({'ads_to0907':0})
f['contrib_fact_0907']=f.amount-f.cogs_fact-f.ads_to0907
ff=f.groupby(['account','n'])[['accr_pos','amount','cogs_fact','ads_to0907','contrib_fact_0907','n_sales_tx']].sum().reset_index()
szn=szn.merge(ff[['account','n','contrib_fact_0907','n_sales_tx']].rename(columns={'n_sales_tx':'sales_tx_0907'}),on=['account','n'])

# search conversion (weeks fully inside 10.08-06.09)
sw=srch[(srch.period_start>='2026-08-10')&(srch.period_end<='2026-09-07')]
sv=sw.groupby(['account','offer_id']).unique_view_users.sum().rename('search_views')
po=p[(p.d>='2026-08-10')&(p.d<='2026-09-06')&(~p.cancel)].groupby(['account','offer_id']).posting_number.nunique().rename('orders_1008_0906')
tmp=off[['account','offer_id','n','grp']].merge(sv,on=['account','offer_id'],how='left').merge(po,on=['account','offer_id'],how='left').fillna(0)
sc=tmp.groupby(['account','n']).agg(search_views=('search_views','sum'),o=('orders_1008_0906','sum')).reset_index()
sc['CR_orders_per_search_view_%']=100*sc.o/sc.search_views.replace(0,np.nan)
szn=szn.merge(sc[['account','n','search_views','CR_orders_per_search_view_%']],on=['account','n'],how='left')
scg=tmp.groupby(['account','n','grp']).agg(search_views=('search_views','sum'),o=('orders_1008_0906','sum')).reset_index()
scg['CR_orders_per_search_view_%']=100*scg.o/scg.search_views.replace(0,np.nan)
sz=sz.merge(scg[['account','n','grp','search_views','CR_orders_per_search_view_%']],on=['account','n','grp'],how='left')

# ---------- stock / stockout ----------
stv=st[(st.d>=P0)&(st.d<=P1)][['d','external_code','avail']]
days=pd.date_range(P0,P1)
fam_bases=off[['account','base']].drop_duplicates()
has_stock=set(stv.external_code)
def stockout(n, bases):
    b=[x for x in bases if x in has_stock]
    s=stv[stv.external_code.isin(b)]
    piv=s.pivot_table(index='external_code',columns='d',values='avail',aggfunc='sum').reindex(columns=days)
    so=(piv.fillna(0)<n)  # missing snapshot day counted as 0 (no record)
    return len(b), len(bases), so.values.mean() if len(b) else np.nan
# stockout weighted to offers that had orders or are in campaign
rows=[]
for (acc,n,grp),g in fullP.groupby(['account','n','grp']):
    nb,ntot,sor=stockout(n, list(g.base.unique()))
    act=g[(g.orders>0)|(g.offer_id.isin(camp_offers))]
    nb2,_,sor2=stockout(n, list(act.base.unique())) if len(act) else (0,0,np.nan)
    rows.append((acc,n,grp,ntot,nb,sor,len(act),sor2))
so=pd.DataFrame(rows,columns=['account','n','grp','bases','bases_with_stock_data','stockout_day_share_all','bases_active','stockout_day_share_active'])
sz=sz.merge(so,on=['account','n','grp'],how='left')
sz.to_csv(O+'size_by_group.csv',index=False); szn.to_csv(O+'size_totals.csv',index=False)

# ---------- weekly ----------
def wkid(d): return ((d-P0).dt.days//7)
wrows=[]
for k in range(6):
    d0=P0+pd.Timedelta(days=7*k); d1=min(d0+pd.Timedelta(days=6),P1)
    x=per_offer(d0,d1,TX_END).merge(off,on=['account','offer_id'],how='right'); x[num]=x[num].fillna(0)
    x['payout_ratio']=[RT[(a,n)][0] for a,n in zip(x.account,x.n)]
    x=econ(x); x['week']=f'{d0:%d.%m}-{d1:%d.%m}'; x['k']=k
    wrows.append(x)
W=pd.concat(wrows)
W[['account','offer_id','base','n','grp','week','k','orders','qty','cart_qty','rev','cancels','views','clicks','money_spent','orders_qty','rev_net_est','cogs','contrib_est']].to_csv(O+'offers_weekly.csv',index=False)
wk=ratios(W.groupby(['account','n','grp','k','week']).apply(agg).reset_index())
wk.to_csv(O+'size_weekly.csv',index=False)

import pickle
pickle.dump(dict(off=off,fullP=fullP,W=W,RT=RT,szn=szn,sz=sz,so=so),open(O+'data/main.pkl','wb'))
pd.set_option('display.width',250)
cols=['account','n','offers_with_orders','views','clicks','money_spent','orders','cart_qty','rev','contrib_est','contrib_fact_0907','CR_orders_per_click_%','cancel_%','return_%_qty','payout_ratio','rate_src']
print(szn[cols].round(2).to_string(index=False))
print(sz[['account','n','grp','offers','offers_with_orders','views','clicks','money_spent','orders','rev','contrib_est','stockout_day_share_active']].round(2).to_string(index=False))

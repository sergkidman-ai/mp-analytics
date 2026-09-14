import pandas as pd, numpy as np
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
D=R+'/data'
A=set(pd.read_csv(D+'/A.csv',dtype=str).iloc[:,0]); B=set(pd.read_csv(D+'/B.csv',dtype=str).iloc[:,0]); REF=set(pd.read_csv(D+'/REF.csv',dtype=str).iloc[:,0])
prod=pd.read_pickle(D+'/prod.pkl'); prod=prod[prod.account=='oz_acc1']
days=pd.date_range('2026-07-27','2026-09-13').date
out=[]
def P(*a):
    s=' '.join(str(x) for x in a); out.append(s); print(s)
ads=pd.read_pickle(D+'/ads.pkl'); ads=ads[ads.account=='oz_acc1']
post=pd.read_pickle(D+'/post.pkl'); post=post[post.account=='oz_acc1']
P('post status',post.status.value_counts().to_dict())
pc=post[post.status!='cancelled'].copy(); pc['rev']=pc.price*pc.qty
# key check
psk=set(pc[pc.d>=pd.Timestamp('2026-07-27').date()].sku); ask=set(ads.sku)
P('post sku since 27.07',len(psk),'in ozon_product acc1',len(psk&set(prod.sku)),'| ads sku',len(ask),'in product',len(ask&set(prod.sku)))
# offer_id -> sku map from product (non archived priority)
allsku=A|B|REF
C='/opt/mp-analytics/docs/experiments/cohorts/'
coh={}
for nm in ['E5_treatment','E5_control','E7_treatment']:
    coh[nm]=set(pd.read_csv(C+nm+'_2026-08-20.csv',dtype=str).sku)
for nm,fn in [('HALO_CORE_A','HALO_CORE_A_STABLE_ADVERTISED_2026-08-21.csv'),('HALO_BASE','HALO_BASELINE_NEVER_ADVERTISED_2026-08-21.csv')]:
    x=pd.read_csv(C+fn,dtype=str); coh[nm]=set(x[x['в_когорте']=='1'].sku)
coh['ASC']=set(pd.read_csv(C+'ACCOUNT_STABLE_CORE_2026-08-20.csv',dtype=str).sku)
import pickle; pickle.dump(coh,open(D+'/coh.pkl','wb'))
P('cohort sizes',{k:len(v) for k,v in coh.items()})
for v in coh.values(): allsku=allsku|v
allsku=allsku|set(pc.sku)|set(ads.sku)
grp={**{s:'A' for s in A},**{s:'B' for s in B},**{s:'REF' for s in REF}}
# postings sku x day
pa=pc.groupby(['sku','d']).agg(orders=('posting_number','nunique'),units=('qty','sum'),rev=('rev','sum')).reset_index()
aa=ads.groupby(['sku','stat_date']).agg(views=('views','sum'),clicks=('clicks','sum'),spend=('spend','sum'),ad_units=('orders_qty','sum'),ad_rev=('orders_money','sum')).reset_index().rename(columns={'stat_date':'d'})
# stock
ss=pd.read_pickle(D+'/ss.pkl'); off=prod[['sku','offer_id']].dropna()
off2=off.copy(); m=off2.offer_id.str.extract(r'^(.+)X(\d+)$')
off2['key']=m[0].fillna(off2.offer_id); off2['mult']=m[1].fillna('1').astype(float)
ss=ss.merge(off2,left_on='external_code',right_on='key')
ss['free']=np.floor(ss.free.astype(float)/ss.mult)
ssa=ss.groupby(['sku','d']).free.sum().reset_index()
fbo=pd.read_pickle(D+'/fbo.pkl'); fbo=fbo[fbo.account=='oz_acc1'].groupby(['sku','d']).fts.sum().reset_index()
pr=pd.read_pickle(D+'/price.pkl')[['offer_id','d','price','marketing_price']]
pr['marketing_price']=pr.marketing_price.astype(float)
pr=pr.merge(off,on='offer_id')[['sku','d','price','marketing_price']].drop_duplicates(['sku','d'])
idx=pd.MultiIndex.from_product([sorted(allsku),days],names=['sku','d']).to_frame(index=False)
for x in (pa,aa,ssa,fbo,pr): x['d']=pd.to_datetime(x['d']).dt.date
pn=idx.merge(pa,how='left').merge(aa,how='left').merge(ssa,how='left').merge(fbo,how='left').merge(pr,how='left')
for c in ['orders','units','rev','views','clicks','spend','ad_units','ad_rev']: pn[c]=pn[c].fillna(0).astype(float)
pn['grp']=pn.sku.map(grp).fillna('OTHER')
pn.to_pickle(D+'/panel.pkl')
P('panel rows',len(pn),'sku',pn.sku.nunique())
# coverage of stock
cov=pn.groupby('grp').apply(lambda g: pd.Series({'sku':g.sku.nunique(),'sku_with_ss_row':g[g.free.notna()].sku.nunique(),'sku_price_row':g[g.price.notna()].sku.nunique(),'sku_in_product':len(set(g.sku)&set(prod.sku))}))
P(cov.to_string())
# totals acc1 ads & postings per week vs panel
ads['wk']=pd.to_datetime(ads.stat_date).dt.to_period('W-SUN')
P('acc1 ads spend by week (all sku):',ads.groupby('wk').spend.sum().round(0).to_dict())
P('ad units vs ad orders sum by grp total:',pn.groupby('grp')[['units','ad_units','orders']].sum().to_dict())
# sanity: ad_units > units days share
P('rows ad_units>units share by grp',pn.assign(x=pn.ad_units>pn.units).groupby('grp').x.mean().round(4).to_dict())
open(R+'/s2_out.txt','w').write('\n'.join(out))

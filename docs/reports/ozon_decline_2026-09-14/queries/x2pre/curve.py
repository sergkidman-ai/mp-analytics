# observational bid -> views/clicks curve, acc1 (read-only, pickles)
import sys, re, pickle
sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles')
from prep import L, split
import pandas as pd, numpy as np
X='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/x2pre/'
ACC='oz_acc1'
prod=L('prod'); prod=prod[prod.account==ACC].drop_duplicates('sku').copy()
prod[['base','n']]=prod.offer_id.apply(lambda o: pd.Series(split(o)))
prod['cart']=prod.name.str.lower().str.startswith('картридж',na=False)
bids=pd.read_pickle(X+'bids_acc1.pkl'); bids['d']=pd.to_datetime(bids.d); bids['sku']=bids.sku.astype(str); bids['campaign_id']=bids.campaign_id.astype(str)
bids=bids[(bids.d>='2026-08-07')&(bids.d<='2026-09-13')]
bids=bids[bids.state.astype(str).str.contains('RUNNING|ACTIVE',na=False)]
ads=L('ads'); ads=ads[ads.account==ACC]
k=['d','campaign_id','sku']
m=bids.merge(ads[k+['views','clicks','money_spent','orders_qty','bid']].rename(columns={'bid':'bid_ads'}),on=k,how='left')
for c in ('views','clicks','money_spent','orders_qty'): m[c]=m[c].fillna(0)
m=m.merge(prod[['sku','offer_id','base','n','cart','name']],on='sku',how='left')
m['bid']=pd.to_numeric(m.bid,errors='coerce')
m['seg']=np.select([(m.n==1)&m.cart,(m.n==2)],['single_cartridge','X2'],'other')
BINS=[0,9,12,15,17.5,25,40,65,1e6]; LAB=['<=9','9-12','12-15','15-17.5','~20(17.5-25)','~30(25-40)','~50(40-65)','>=65(~80)']
m['bin']=pd.cut(m.bid,BINS,labels=LAB,include_lowest=True)
def summ(g):
    return pd.Series(dict(sku_days=len(g),skus=g.sku.nunique(),views_mean=g.views.mean(),views_med=g.views.median(),views_p75=g.views.quantile(.75),
        share_views0=(g.views==0).mean(),clicks_per_day=g.clicks.mean(),CTR_pct=100*g.clicks.sum()/max(g.views.sum(),1),
        CPC=g.money_spent.sum()/max(g.clicks.sum(),1),spend_per_day=g.money_spent.mean(),ad_orders_per_click_pct=100*g.orders_qty.sum()/max(g.clicks.sum(),1)))
R=m[m.seg!='other'].groupby(['seg','bin']).apply(summ).reset_index()
R.to_csv(X+'bid_curve_bins.csv',index=False)
# within-SKU elasticity (SKU x campaign FE, day FE) for SKUs whose bid changed
m['lb']=np.log(m.bid.clip(lower=1)); m['lv']=np.log1p(m.views); m['key']=m.campaign_id+'_'+m.sku
out={}
for seg in ('single_cartridge','X2','all'):
    g=m if seg=='all' else m[m.seg==seg]
    var=g.groupby('key').bid.transform(lambda s:s.max()/max(s.min(),0.01))
    g=g[var>=1.2].copy()
    if len(g)<50: out[seg]=(len(g),np.nan); continue
    for c in ('lb','lv'):
        g[c+'_dm']=g[c]-g.groupby('key')[c].transform('mean')
        g[c+'_dm']=g[c+'_dm']-g.groupby('d')[c+'_dm'].transform('mean')
    beta=(g.lb_dm*g.lv_dm).sum()/(g.lb_dm**2).sum()
    out[seg]=(g.key.nunique(),round(float(beta),3))
pickle.dump(dict(R=R,elast=out),open(X+'curve.pkl','wb'))
pd.set_option('display.width',220)
cols=['seg','bin','sku_days','skus','views_mean','views_med','share_views0','clicks_per_day','CTR_pct','CPC','ad_orders_per_click_pct']
print(R[cols].round(2).to_csv(sep='|',index=False))
print('within-SKU elasticity d log(1+views)/d log(bid) (n keys, beta):',out)

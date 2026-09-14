import pandas as pd, numpy as np, pickle
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
D=R+'/data'
pn=pd.read_pickle(D+'/panel.pkl'); pn['d']=pd.to_datetime(pn.d)
pn['avail']=((pn.free.fillna(0)>0)|(pn.fts.fillna(0)>0)).astype(float)
PER={'pre_27.07-08.08':('2026-07-27','2026-08-08'),'off_10-18.08':('2026-08-10','2026-08-18'),
     'post_20.08-13.09':('2026-08-20','2026-09-13'),'post1_20.08-01.09':('2026-08-20','2026-09-01'),'post2_05-13.09':('2026-09-05','2026-09-13')}
rows=[]
for g in ['A','B','REF','ALL_ACC1']:
    x=pn if g=='ALL_ACC1' else pn[pn.grp==g]
    nsku=x.sku.nunique()
    for p,(a,b) in PER.items():
        y=x[(x.d>=a)&(x.d<=b)]; nd=y.d.nunique()
        s=y[['views','clicks','spend','ad_units','ad_rev','orders','units','rev']].sum()
        r={'group':g,'period':p,'days':nd,'sku':nsku}
        for k,v in s.items(): r[k]=round(v,1)
        r['nonad_units']=round(s.units-s.ad_units,1); r['nonad_rev']=round(s.rev-s.ad_rev,0)
        r['sku_in_ads_share']=round(y[y.views>0].sku.nunique()/nsku,3)
        r['sku_with_sales']=y[y.units>0].sku.nunique()
        r['avail_share']=round(y.avail.mean(),3)
        for k in ['spend','ad_units','ad_rev','orders','units','rev','nonad_units']:
            r[k+'_per_wk']=round((s[k] if k!='nonad_units' else s.units-s.ad_units)*7/nd,1)
        r['drr_ad']=round(s.spend/s.ad_rev,3) if s.ad_rev else None
        r['drr_total']=round(s.spend/s.rev,3) if s.rev else None
        rows.append(r)
T=pd.DataFrame(rows); T.to_csv(R+'/p1_periods.csv',index=False)
# search weekly
se=pd.read_pickle(D+'/search.pkl'); se=se[se.account=='oz_acc1']
grp=pn[['sku','grp']].drop_duplicates()
se=se.merge(grp,on='sku',how='left')
W=se.groupby(['grp','period_start']).agg(sku=('sku','nunique'),view_users=('unique_view_users','sum'),search_users=('unique_search_users','sum'),orders=('order_count','sum'),gmv=('gmv','sum'),pos_med=('position','median')).reset_index()
Wall=se.groupby('period_start').agg(sku=('sku','nunique'),view_users=('unique_view_users','sum'),search_users=('unique_search_users','sum'),orders=('order_count','sum'),gmv=('gmv','sum'),pos_med=('position','median')).reset_index(); Wall['grp']='ALL_ACC1'
W=pd.concat([W,Wall]); W=W[W.period_start>=pd.Timestamp('2026-07-20').date()]
W.to_csv(R+'/p1_search_weekly.csv',index=False)
pd.set_option('display.width',250)
print(T[['group','period','days','views','clicks','spend','ad_units','ad_rev','orders','units','rev','nonad_units','sku_with_sales','avail_share','drr_ad']].to_string(index=False))
print(W[W.grp.isin(['A','B','REF'])].pivot(index='period_start',columns='grp',values='view_users').to_string())

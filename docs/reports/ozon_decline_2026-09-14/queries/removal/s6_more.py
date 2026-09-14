import pandas as pd, numpy as np, pickle
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
D=R+'/data'
out=[]
def P(*a):
    s=' '.join(str(x) for x in a); out.append(s); print(s)
rng=np.random.default_rng(11)
def boot(dA,dB,n=5000):
    dA=np.asarray(dA,float); dB=np.asarray(dB,float)
    bs=dA[rng.integers(0,len(dA),(n,len(dA)))].mean(1)-dB[rng.integers(0,len(dB),(n,len(dB)))].mean(1)
    return dA.mean()-dB.mean(), np.percentile(bs,2.5), np.percentile(bs,97.5)
pn=pd.read_pickle(D+'/panel_tx.pkl'); coh=pickle.load(open(D+'/coh.pkl','rb'))
# E5 contribution-estimate DiD (rev*crate - spend), crate=0.31 (Aug tx, both groups)
pn['cest']=pn.rev*0.312-pn.spend
def m(sk,a,b): return pn[pn.sku.isin(sk)&(pn.d>=a)&(pn.d<=b)].groupby('sku')[['orders','rev','spend','cest']].mean()
T,C=coh['E5_treatment'],coh['E5_control']
for base in [('2026-08-11','2026-08-17'),('2026-07-27','2026-08-17')]:
    for wa,wb in [('2026-08-18','2026-09-01'),('2026-08-18','2026-09-13')]:
        dT=m(T,wa,wb)-m(T,*base); dC=m(C,wa,wb)-m(C,*base)
        for k in ['orders','rev','cest']:
            e,lo,hi=boot(dT[k],dC[k]); P(f'E5 base {base[0][5:]}..{base[1][5:]} win {wa[5:]}..{wb[5:]} {k}: {e:.2f}/SKU/day [{lo:.2f};{hi:.2f}] T14/wk {e*98:.0f} [{lo*98:.0f};{hi*98:.0f}]')
# E5 T14 cumulative 18.08-13.09 vs its own spend
x=pn[pn.sku.isin(T)&(pn.d>='2026-08-18')]
P('E5 T14 18.08-13.09: spend',round(x.spend.sum()),'ad_rev',round(x.ad_rev.sum()),'post rev',round(x.rev.sum()),'orders',int(x.orders.sum()),'DRR ad',round(x.spend.sum()/x.ad_rev.sum(),3),'DRR total',round(x.spend.sum()/x.rev.sum(),3))
# ---- E4 rollback before/after (acc1) ----
e4=pd.read_csv('/opt/mp-analytics/docs/reports/ozon_e4_rollback_replay_2026-08-20.csv',dtype=str)
rb=set(e4[e4['факт_действие']=='rollback'].sku)-coh['E7_treatment']
for a,b,lab in [('2026-08-10','2026-08-17','before 10-17.08'),('2026-08-19','2026-09-01','after 19.08-01.09'),('2026-09-05','2026-09-13','05-13.09')]:
    y=pn[pn.sku.isin(rb)&(pn.d>=a)&(pn.d<=b)]; nd=y.d.nunique()
    P('E4 rollback',len(rb),'SKU',lab,'orders/wk',round(y.orders.sum()*7/nd,1),'rev/wk',round(y.rev.sum()*7/nd),'spend/wk',round(y.spend.sum()*7/nd),'ad_units/wk',round(y.ad_units.sum()*7/nd,1),'views/wk',round(y.views.sum()*7/nd))
# ---- E6 acc2 operational ----
ads=pd.read_pickle(D+'/ads.pkl'); ads=ads[ads.account=='oz_acc2']; ads['stat_date']=pd.to_datetime(ads.stat_date)
post=pd.read_pickle(D+'/post.pkl'); post=post[(post.account=='oz_acc2')&(post.status!='cancelled')]; post['d']=pd.to_datetime(post.d); post['rev']=post.price.astype(float)*post.qty.astype(float)
rem=pd.read_csv('/opt/mp-analytics/docs/reports/ozon_acc2_e1_removed.csv',dtype=str)
T6=set(pd.read_csv('/opt/mp-analytics/docs/experiments/cohorts/E6_treatment_2026-08-20.csv',dtype=str).sku); C6=set(pd.read_csv('/opt/mp-analytics/docs/experiments/cohorts/E6_control_2026-08-20.csv',dtype=str).sku)
R6=set(rem.sku)
P('E6 removed csv pairs',len(rem),'sku',len(R6),'in E6_T',len(R6&T6))
for sk,lab in [(R6,'removed'),(C6,'control93')]:
    for a,b,pl in [('2026-08-11','2026-08-17','base'),('2026-08-19','2026-09-01','post1'),('2026-09-05','2026-09-13','post2')]:
        nd=(pd.Timestamp(b)-pd.Timestamp(a)).days+1
        ad=ads[ads.sku.isin(sk)&(ads.stat_date>=a)&(ads.stat_date<=b)]; po=post[post.sku.isin(sk)&(post.d>=a)&(post.d<=b)]
        P('E6',lab,len(sk),pl,'units/wk',round(po.qty.astype(float).sum()*7/nd,1),'rev/wk',round(po.rev.sum()*7/nd),'spend/wk',round(ad.spend.astype(float).sum()*7/nd),'ad_units/wk',round(ad.orders_qty.astype(float).sum()*7/nd,1))
open(R+'/s6_out.txt','w').write('\n'.join(out))

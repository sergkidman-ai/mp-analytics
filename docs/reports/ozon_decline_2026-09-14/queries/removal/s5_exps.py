import pandas as pd, numpy as np, pickle
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
D=R+'/data'
out=[]
def P(*a):
    s=' '.join(str(x) for x in a); out.append(s); print(s)
rng=np.random.default_rng(7)
def boot(dA,dB,n=5000):
    dA=np.asarray(dA,float); dB=np.asarray(dB,float)
    bs=dA[rng.integers(0,len(dA),(n,len(dA)))].mean(1)-dB[rng.integers(0,len(dB),(n,len(dB)))].mean(1)
    return dA.mean()-dB.mean(), np.percentile(bs,2.5), np.percentile(bs,97.5)
pn=pd.read_pickle(D+'/panel.pkl'); pn['d']=pd.to_datetime(pn.d)
pn['avail']=((pn.free.fillna(0)>0)|(pn.fts.fillna(0)>0)).astype(float)
coh=pickle.load(open(D+'/coh.pkl','rb'))
# ---------- transactions per sku-day (acc1) ----------
tx=pd.read_pickle(D+'/tx.pkl'); tx=tx[tx.account=='oz_acc1']
ti=tx[tx.nitems>0].copy(); ti['amount']=ti.amount.astype(float)/ti.nitems; ti['accr']=ti.accr.astype(float)/ti.nitems
ti['d']=pd.to_datetime(ti.d)
P('tx item rows',len(ti),'sku in panel',ti.sku.isin(set(pn.sku)).mean().round(3),'max date',ti.d.max().date())
mbs=pd.read_pickle(D+'/mbs.pkl'); mbs=mbs[mbs.account=='oz_acc1'].copy()
for c in ['revenue_buyer','cogs']: mbs[c]=mbs[c].astype(float)
mbs['m']=pd.to_datetime(mbs.period_from).dt.to_period('M')
mbs['ratio']=np.where(mbs.revenue_buyer>0,mbs.cogs/mbs.revenue_buyer,np.nan)
accm=mbs.groupby('m').apply(lambda g:g.cogs.sum()/g.revenue_buyer.sum())
P('acc1 cogs/revenue by month',accm.round(3).to_dict())
ti['m']=ti.d.dt.to_period('M')
ti=ti.merge(mbs[['sku','m','ratio']],how='left',on=['sku','m'])
ti['ratio']=ti.ratio.fillna(ti.m.map(accm)).clip(0,1.5)
ti['cogs']=ti.ratio*ti.accr
txd=ti.groupby(['sku','d']).agg(saldo=('amount','sum'),accr=('accr','sum'),cogs=('cogs','sum')).reset_index()
pn=pn.merge(txd,how='left',on=['sku','d'])
for c in ['saldo','accr','cogs']: pn[c]=pn[c].fillna(0)
pn['contrib']=pn.saldo-pn.cogs-pn.spend
pn.to_pickle(D+'/panel_tx.pkl')
# contribution rate (saldo-cogs)/accr, August, by cohort (for postings-based extrapolation)
def crate(sk):
    x=pn[pn.sku.isin(sk)&(pn.d>='2026-08-01')&(pn.d<='2026-09-07')]
    return (x.saldo.sum()-x.cogs.sum())/x.accr.sum() if x.accr.sum()>0 else np.nan
# ---------- Part1 contribution by group/period (tx-based, to 07.09) ----------
PER={'pre_27.07-08.08':('2026-07-27','2026-08-08'),'off_10-18.08':('2026-08-10','2026-08-18'),'post1_20.08-01.09':('2026-08-20','2026-09-01'),'post2_05-07.09':('2026-09-05','2026-09-07')}
rows=[]
for g in ['A','B','REF']:
    for p,(a,b) in PER.items():
        x=pn[(pn.grp==g)&(pn.d>=a)&(pn.d<=b)]
        rows.append(dict(group=g,period=p,accr=x.accr.sum(),saldo=x.saldo.sum(),cogs=x.cogs.sum(),spend=x.spend.sum(),contrib=x.contrib.sum(),post_rev=x.rev.sum(),ad_rev=x.ad_rev.sum()))
C=pd.DataFrame(rows).round(0); C.to_csv(R+'/p1_contribution.csv',index=False)
for r in C.itertuples(): P('contrib',r.group,r.period,'accr',r.accr,'saldo',r.saldo,'cogs',r.cogs,'spend',r.spend,'contrib',r.contrib)
# ---------- E5 weekly ----------
pn['wk']=pn.d.dt.to_period('W-SUN').apply(lambda p:p.start_time.date())
def weekly(sk,label):
    x=pn[pn.sku.isin(sk)].groupby('wk').agg(orders=('orders','sum'),units=('units','sum'),rev=('rev','sum'),spend=('spend','sum'),ad_units=('ad_units','sum'),ad_rev=('ad_rev','sum'),views=('views','sum'),clicks=('clicks','sum'),accr=('accr','sum'),saldo=('saldo','sum'),cogs=('cogs','sum'),avail=('avail','mean')).reset_index()
    cr=crate(sk)
    x['contrib_tx']=x.saldo-x.cogs-x.spend
    x['contrib_est_postings']=x.rev*cr-x.spend
    x['crate_aug']=cr; x['cohort']=label; x['n_sku']=len(sk)
    return x
acc_sp=pn.groupby('wk').spend.sum()
W=pd.concat([weekly(coh['E5_treatment'],'E5_T14'),weekly(coh['E5_control'],'E5_C26'),weekly(coh['E7_treatment'],'E7_21'),weekly(coh['ASC'],'ACCOUNT_STABLE_CORE_81'),weekly(set(pn.sku),'ACC1_ALL')])
W['acc_spend_share']=W.spend/W.wk.map(acc_sp)
W.round(3).to_csv(R+'/e5_e7_weekly.csv',index=False)
for r in W[W.cohort.isin(['E5_T14','E5_C26'])].itertuples():
    P(r.cohort,r.wk,'ord',int(r.orders),'units',int(r.units),'rev',round(r.rev),'spend',round(r.spend),'adrev',round(r.ad_rev),'share',round(r.acc_spend_share,2),'contrib_tx',round(r.contrib_tx),'contrib_est',round(r.contrib_est_postings),'crate',round(r.crate_aug,3))
# E5 DiD per SKU-day: baseline 11-17.08, window 18.08-07.09 and 18.08-13.09
def m(sk,a,b):
    return pn[pn.sku.isin(sk)&(pn.d>=a)&(pn.d<=b)].groupby('sku')[['orders','rev','spend','contrib']].mean().assign(net=lambda x:x.rev-x.spend)
for wa,wb in [('2026-08-18','2026-09-07'),('2026-08-18','2026-09-13')]:
    dT=m(coh['E5_treatment'],wa,wb)-m(coh['E5_treatment'],'2026-08-11','2026-08-17')
    dC=m(coh['E5_control'],wa,wb)-m(coh['E5_control'],'2026-08-11','2026-08-17')
    for k in ['orders','rev','spend','net']:
        e,lo,hi=boot(dT[k],dC[k]); P(f'E5 DiD {wa}..{wb} {k}: {e:.2f}/SKU/day CI[{lo:.2f};{hi:.2f}] -> T14 x7 = {e*14*7:.0f} [{lo*14*7:.0f};{hi*14*7:.0f}]')
# ---------- E7 before/after ----------
sk=coh['E7_treatment']
for a,b,lab in [('2026-08-12','2026-08-18','base'),('2026-08-19','2026-09-01','win'),('2026-09-02','2026-09-13','after')]:
    x=pn[pn.sku.isin(sk)&(pn.d>=a)&(pn.d<=b)]; nd=x.d.nunique()
    P('E7',lab,'orders/SKU-day',round(x.orders.sum()/(nd*len(sk)),4),'rev/wk',round(x.rev.sum()*7/nd),'spend/wk',round(x.spend.sum()*7/nd),'ad_rev/wk',round(x.ad_rev.sum()*7/nd),'avail',round(x.avail.mean(),3))
x=pn[pn.sku.isin(sk)&(pn.d>='2026-07-27')&(pn.d<='2026-08-08')]; P('E7 pre-wave(27.07-08.08) orders/SKU-day',round(x.orders.sum()/(13*len(sk)),4),'rev/wk',round(x.rev.sum()*7/13),'spend/wk',round(x.spend.sum()*7/13))
# ---------- H-001 ----------
for lab,(a,b) in {'A_27.07-08.08':('2026-07-27','2026-08-08'),'B_10-18.08':('2026-08-10','2026-08-18'),'C1_20.08-01.09':('2026-08-20','2026-09-01'),'C2_05-13.09':('2026-09-05','2026-09-13')}.items():
    r={}
    for nm in ['HALO_CORE_A','HALO_BASE']:
        x=pn[pn.sku.isin(coh[nm])&(pn.d>=a)&(pn.d<=b)]
        av=x[x.avail>0]; r[nm]=(av.orders.sum()/len(av)*100 if len(av) else np.nan, len(av), x.orders.sum(), x.rev.sum())
    P('H-001',lab,'CORE_A ord/100 avail SKU-days',round(r['HALO_CORE_A'][0],2),'(avail days',r['HALO_CORE_A'][1],'orders',int(r['HALO_CORE_A'][2]),') BASE',round(r['HALO_BASE'][0],2),'(avail days',r['HALO_BASE'][1],'orders',int(r['HALO_BASE'][2]),') ratio',round(r['HALO_CORE_A'][0]/r['HALO_BASE'][0],2) if r['HALO_BASE'][0] else None)
open(R+'/s5_out.txt','w').write('\n'.join(out))

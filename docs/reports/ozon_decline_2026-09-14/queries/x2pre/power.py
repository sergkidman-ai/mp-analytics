# MDE (ANCOVA on family-period) + A/A placebo on historical windows (read-only)
import sys, pickle
sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles')
from prep import L, split
import pandas as pd, numpy as np
X='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/x2pre/'
E=pickle.load(open(X+'elig_final.pkl','rb')); B=E['B']; POUT=E['POUT']
rng=np.random.default_rng(20260914)
p=L('post'); p=p[(p.account=='oz_acc1')&(p.status!='cancelled')].copy()
p['fam']=p.offer_id.str[:4]; p[['b','n']]=p.offer_id.apply(lambda o: pd.Series(split(o)))
UC=B.set_index('base').uc_cons
p=p[p.fam.isin(B.base)]
p['rev']=p.price*p.qty; p['cart']=p.qty*p.n; p['sgl']=np.where(p.offer_id==p.fam,p.qty,0)
p['contrib']=p.rev*POUT-p.cart*p.fam.map(UC)
days=pd.date_range('2026-05-01','2026-09-13')
MET=['rev','contrib','cart','sgl']
panel={m:p.pivot_table(index='fam',columns='d',values=m,aggfunc='sum').reindex(columns=days).fillna(0) for m in MET}
Z=(1.959964+0.841621)
def run(universe_col, L_, p_t=0.5, reps=400):
    fams=B[B[universe_col]].base.tolist(); camp=B.set_index('base').in_35269713
    starts=[s for s in pd.date_range('2026-06-01','2026-09-13',freq='7D') if s+pd.Timedelta(days=L_-1)<=pd.Timestamp('2026-09-13')]
    out=[]
    for s in starts:
        post=slice(s, s+pd.Timedelta(days=L_-1)); pre=slice(s-pd.Timedelta(days=28), s-pd.Timedelta(days=1)); strat_w=slice(s-pd.Timedelta(days=56), s-pd.Timedelta(days=1))
        base=panel['rev'].reindex(fams).fillna(0).loc[:,strat_w].sum(axis=1)
        terc=pd.qcut(base.rank(method='first'),3,labels=False)
        strata=(terc.astype(str)+'_'+camp.reindex(fams).astype(int).astype(str)).values
        Sd=pd.get_dummies(strata).values.astype(float)
        for m in MET:
            P=panel[m].reindex(fams).fillna(0)
            Y=P.loc[:,post].sum(axis=1).values; Xc=P.loc[:,pre].sum(axis=1).values*L_/28
            A=np.column_stack([Sd,Xc]); beta,*_=np.linalg.lstsq(A,Y,rcond=None); res=Y-A@beta
            n=len(Y); s2=res.var(ddof=A.shape[1])
            mde=Z*np.sqrt(s2/(n*p_t*(1-p_t))); meanY=Y.mean()
            # A/A: stratified random assignment, Welch t on residuals; naive family-day t-test
            rej=rejn=pw=0
            daily=P.loc[:,post].values
            for _ in range(reps):
                T=np.zeros(n,bool)
                for st in np.unique(strata):
                    idx=np.where(strata==st)[0]; rng.shuffle(idx); T[idx[:int(round(len(idx)*p_t))]]=True
                if T.sum()<2 or (~T).sum()<2: continue
                def welch(v):
                    a,b=v[T],v[~T]; se=np.sqrt(a.var(ddof=1)/len(a)+b.var(ddof=1)/len(b)); return (a.mean()-b.mean())/se if se>0 else 0
                rej+=abs(welch(res))>1.96
                rejn+=abs(welch(np.where(T[:,None],daily,np.nan)[T].ravel()*0+0) if False else 0)>1.96
                a=daily[T].ravel(); b=daily[~T].ravel(); se=np.sqrt(a.var(ddof=1)/len(a)+b.var(ddof=1)/len(b)); rejn+= (abs((a.mean()-b.mean())/se)>1.96) if se>0 else 0
                r2=res+np.where(T,mde,0); pw+=abs(welch(r2))>1.96
            out.append(dict(universe=universe_col,L=L_,p_t=p_t,start=s.date(),metric=m,n=n,meanY=meanY,mde_abs=mde,mde_pct=100*mde/meanY if meanY else np.nan,
                            rho=np.corrcoef(Y,Xc)[0,1] if Y.std()>0 and Xc.std()>0 else np.nan,fpr_ancova=rej/reps,fpr_naive_daylevel=rejn/reps,power_at_mde_sim=pw/reps,zero_share=(Y==0).mean()))
    return out
rows=[]
for u in ('eligible_strict','eligible_E8stratum','eligible_th1_E8stratum'):
    for L_ in (14,28):
        for pt in (0.5,0.6):
            rows+=run(u,L_,pt,reps=300 if pt==0.5 else 100)
R=pd.DataFrame(rows); R.to_csv(X+'power_windows.csv',index=False)
S=R.groupby(['universe','L','p_t','metric']).agg(n=('n','first'),windows=('start','nunique'),meanY=('meanY','median'),mde_abs=('mde_abs','median'),mde_pct=('mde_pct','median'),
   rho=('rho','median'),fpr_ancova=('fpr_ancova','mean'),fpr_naive=('fpr_naive_daylevel','mean'),power_sim=('power_at_mde_sim','mean'),zero=('zero_share','median')).reset_index()
S['nT']=(S.n*S.p_t).round(); S['mde_cohort_abs']=S.mde_abs*S.nT; S['mde_per_family_day']=S.mde_abs/S.L
S.to_csv(X+'power_summary.csv',index=False)
print(S[(S.metric.isin(['rev','contrib','cart','sgl']))][['universe','L','p_t','metric','n','windows','meanY','mde_abs','mde_pct','rho','fpr_ancova','fpr_naive','power_sim','zero','mde_cohort_abs']].round(2).to_csv(sep='|',index=False))

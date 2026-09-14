import sys; sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles')
from prep import *
import pickle
O='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles/'
M=pickle.load(open(O+'data/main.pkl','rb')); off=M['off']
rng=np.random.default_rng(20260914)
acc='oz_acc1'
p=L('post'); p=p[(p.account==acc)&(p.status!='cancelled')]
p[['base','n']]=p.offer_id.apply(lambda o: pd.Series(split(o)))
p['cart']=p.qty*p.n; p['rev']=p.price*p.qty
ads=L('ads').merge(prod[['account','sku','offer_id']],on=['account','sku']); ads=ads[ads.account==acc]
ads[['base','n']]=ads.offer_id.apply(lambda o: pd.Series(split(o)))
price=L('price'); price=price[price.account==acc]
price[['base','n']]=price.offer_id.apply(lambda o: pd.Series(split(o)))

bases_b=bases_by_acc[acc]
single_offers=set(prod[(prod.account==acc)&(prod.n==1)].offer_id)
T=CAMP_BASES; C1=LIST193-CAMP_BASES
# universe: 4-digit bases that have a single card
uni=sorted(b for b in single_offers if re.match(r'^\d{4}$',b))
grp={b:('T' if b in T else 'C1' if b in C1 else 'C2' if b in bases_b else 'C3') for b in uni}

WIN={'pre_b':('2026-06-29','2026-07-26'),'prepre':('2026-06-13','2026-07-10'),'pre':('2026-07-11','2026-08-07'),'post':('2026-08-08','2026-09-04'),'post_clean':('2026-08-08','2026-09-01'),'post_full':('2026-08-08','2026-09-13')}
def fam(win, only_single=False):
    d0,d1=map(pd.Timestamp,WIN[win]); nd=(d1-d0).days+1
    a=p[(p.d>=d0)&(p.d<=d1)&p.base.isin(uni)]
    if only_single: a=a[a.n==1]
    g=a.groupby('base').agg(cart=('cart','sum'),rev=('rev','sum'))
    return (g.reindex(uni).fillna(0)/nd)*28   # per 28 days
hist=p[(p.d>='2026-05-01')&(p.d<='2026-07-10')&p.base.isin(uni)].groupby('base').cart.sum().reindex(uni).fillna(0)
df=pd.DataFrame({'grp':pd.Series(grp),'h0':hist})
for w in WIN:
    f=fam(w); s=fam(w,True)
    df[f'fam_{w}']=f.cart; df[f'sgl_{w}']=s.cart; df[f'sglrev_{w}']=s.rev; df[f'famrev_{w}']=f.rev
# strata by hist cartridges (May1-Jul10) using T quantiles
tq=np.quantile(df.loc[df.grp=='T','h0'],[0,.2,.4,.6,.8,1.0])
BINS=[-0.5,0.5,1.5,2.5,4.5,8.5,16.5,32.5,1e9]
df['stratum']=pd.cut(df.h0,BINS,labels=False)
df['in_range']=df.h0>=1
# ad exposure of singles pre (27.07-07.08) vs post (08.08-04.09) per day
def adx(d0,d1):
    a=ads[(ads.d>=d0)&(ads.d<=d1)&(ads.n==1)]; nd=(pd.Timestamp(d1)-pd.Timestamp(d0)).days+1
    return a.groupby('base')[['views','money_spent']].sum().reindex(uni).fillna(0)/nd
ax0=adx('2026-07-27','2026-08-07'); ax1=adx('2026-08-08','2026-09-04')
df['adv_pre_day']=ax0.views; df['adv_post_day']=ax1.views; df['adsp_pre_day']=ax0.money_spent; df['adsp_post_day']=ax1.money_spent
pr=price[price.n==1].groupby('base').apply(lambda g:(g.p_after/g.p_before).median()).reindex(uni)
df['price_chg_0904']=pr
df.to_csv(O+'did_families.csv')

def weights(ctrl):
    # reweight control to T stratum distribution
    t=df[(df.grp=='T')&df.in_range].stratum.value_counts(normalize=True)
    c=ctrl.stratum.value_counts(normalize=True)
    return ctrl.stratum.map(t/c).fillna(0)
def did(ctrlname, y, post='post', pre='pre', B=2000):
    Tt=df[(df.grp=='T')&df.in_range] if ctrlname in ('C2m','C3m') else df[df.grp=='T']
    if ctrlname=='B_vs_C3':
        Tt=df[df.grp.isin(['T','C1','C2'])&df.in_range]; Cc=df[(df.grp=='C3')&df.in_range]
        t=Tt.stratum.value_counts(normalize=True); c=Cc.stratum.value_counts(normalize=True); w=Cc.stratum.map(t/c).fillna(0)
    elif ctrlname=='C2m': Cc=df[(df.grp=='C2')&df.in_range]
    elif ctrlname=='C3m': Cc=df[(df.grp=='C3')&df.in_range]
    else: Cc=df[df.grp==ctrlname]
    if ctrlname!='B_vs_C3': w=weights(Cc) if ctrlname in ('C2m','C3m') else pd.Series(1.0,index=Cc.index)
    def est(Ti,Ci,wi):
        dT=(Ti[f'{y}_{post}']-Ti[f'{y}_{pre}']).mean()
        dC=np.average(Ci[f'{y}_{post}']-Ci[f'{y}_{pre}'],weights=wi) if wi.sum()>0 else np.nan
        baseT=Ti[f'{y}_{pre}'].mean()
        return dT-dC, dT, dC, baseT
    e=est(Tt,Cc,w)
    bs=[]
    for _ in range(B):
        ti=Tt.sample(len(Tt),replace=True,random_state=rng.integers(1e9)); ci=Cc.sample(len(Cc),replace=True,random_state=rng.integers(1e9))
        bs.append(est(ti,ci,w.loc[ci.index])[0])
    lo,hi=np.nanpercentile(bs,[2.5,97.5])
    return dict(control=ctrlname,y=y,pre=pre,post=post,nT=len(Tt),nC=len(Cc),T_pre=round(e[3],2),dT=round(e[1],2),dC=round(e[2],2),DiD=round(e[0],2),DiD_pct_of_Tpre=round(100*e[0]/e[3],1) if e[3] else np.nan,ci95_lo=round(lo,2),ci95_hi=round(hi,2))
res=[]
for ctrl in ('C1','C2m','C3m'):
    if ctrl=='C3m': continue
    for y in ('fam','sgl','sglrev'):
        res.append(did(ctrl,y))
        res.append(did(ctrl,y,post='post_clean'))
    res.append(did(ctrl,'fam',post='pre',pre='prepre'))   # placebo
    res.append(did(ctrl,'sgl',post='pre',pre='prepre'))
res.append(did('B_vs_C3','fam',post='post',pre='pre_b')); res.append(did('B_vs_C3','sgl',post='post',pre='pre_b')); res.append(did('B_vs_C3','fam',post='pre_b',pre='prepre')); res.append(did('B_vs_C3','famrev',post='post',pre='pre_b')); res.append(did('B_vs_C3','sglrev',post='post',pre='pre_b')); res.append(did('B_vs_C3','fam',post='post_clean',pre='pre_b')); res.append(did('B_vs_C3','sgl',post='pre_b',pre='prepre'))
R=pd.DataFrame(res); R.to_csv(O+'did_results.csv',index=False)
pd.set_option('display.width',250)
print(R.to_string(index=False))
g=df.groupby('grp').agg(n=('h0','size'),h0=('h0','mean'),fam_pre=('fam_pre','mean'),fam_post=('fam_post','mean'),sgl_post=('sgl_post','mean'),adv_pre=('adv_pre_day','mean'),adv_post=('adv_post_day','mean'),sp_pre=('adsp_pre_day','mean'),sp_post=('adsp_post_day','mean'),price_chg=('price_chg_0904','median'))
print(g.round(2).to_string())
# bundle share of family cartridges in T post
t=df[df.grp=='T']; print('T bundle share of fam cart post:', round(1-t.sgl_post.sum()/t.fam_post.sum(),3), ' C1:', round(1-df[df.grp=='C1'].sgl_post.sum()/df[df.grp=='C1'].fam_post.sum(),3), ' C2:', round(1-df[df.grp=='C2'].sgl_post.sum()/max(df[df.grp=='C2'].fam_post.sum(),1),3))

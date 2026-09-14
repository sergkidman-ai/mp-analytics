import pandas as pd, numpy as np, pickle, json
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
D=R+'/data'
pn=pd.read_pickle(D+'/panel.pkl'); pn['d']=pd.to_datetime(pn.d)
pn['avail']=((pn.free.fillna(0)>0)|(pn.fts.fillna(0)>0)).astype(float)
pn['net']=pn.rev-pn.spend
AB=pn[pn.grp.isin(['A','B'])].copy()
out=[]; res={}
def P(*a):
    s=' '.join(str(x) for x in a); out.append(s); print(s)
def win(a,b): return AB[(AB.d>=a)&(AB.d<=b)].groupby(['sku','grp'])[['orders','units','rev','spend','net','avail','views']].mean()
pre1=win('2026-07-27','2026-08-08'); pre2=win('2026-08-10','2026-08-18')
posts={'post1_20.08-01.09':win('2026-08-20','2026-09-01'),'post_20.08-13.09':win('2026-08-20','2026-09-13'),'post2_05-13.09':win('2026-09-05','2026-09-13')}
rng=np.random.default_rng(20260914)
def boot(dA,dB,n=5000):
    dA=np.asarray(dA); dB=np.asarray(dB)
    iA=rng.integers(0,len(dA),(n,len(dA))); iB=rng.integers(0,len(dB),(n,len(dB)))
    bs=dA[iA].mean(1)-dB[iB].mean(1)
    return dA.mean()-dB.mean(), np.percentile(bs,2.5), np.percentile(bs,97.5), (bs<=0).mean()
# balance
P('== BALANCE (per SKU-day means, A vs B) ==')
for nm,w in [('pre1 27.07-08.08',pre1),('pre2 10-18.08',pre2)]:
    for m in ['orders','units','rev','spend','avail','views']:
        a=w.xs('A',level='grp')[m]; b=w.xs('B',level='grp')[m]
        est,lo,hi,_=boot(a,b,2000)
        P(f'{nm} {m}: A={a.mean():.4f} B={b.mean():.4f} diff={est:.4f} CI[{lo:.4f};{hi:.4f}]')
# DiD
P('== DiD A-B (post - pre2), bootstrap by SKU, 5000 ==')
rows=[]
for pnm,pw in posts.items():
    for base_nm,bw in [('pre2',pre2),('pre1',pre1)]:
        dd=(pw-bw)
        for m in ['orders','units','rev','net','spend']:
            a=dd.xs('A',level='grp')[m]; b=dd.xs('B',level='grp')[m]
            est,lo,hi,p0=boot(a,b)
            rows.append(dict(post=pnm,base=base_nm,metric=m,A_diff=a.mean(),B_diff=b.mean(),did=est,lo=lo,hi=hi,p_le0=p0,
                             B_week_total=est*len(b)*7,B_week_lo=lo*len(b)*7,B_week_hi=hi*len(b)*7))
            if base_nm=='pre2' or m in('orders','rev'):
                P(f'{pnm} vs {base_nm} {m}: dA={a.mean():.4f} dB={b.mean():.4f} DiD={est:.4f}/SKU/day CI[{lo:.4f};{hi:.4f}] P(<=0)={p0:.3f} -> B*7d={est*len(b)*7:.0f} [{lo*len(b)*7:.0f};{hi*len(b)*7:.0f}]')
pd.DataFrame(rows).round(5).to_csv(R+'/e8_did.csv',index=False)
# placebo: pre1->pre2 change (both groups lost ads together) should be equal
dd=pre2-pre1
for m in ['orders','rev']:
    a=dd.xs('A',level='grp')[m]; b=dd.xs('B',level='grp')[m]; est,lo,hi,_=boot(a,b,2000)
    P(f'placebo pre1->pre2 {m}: DiD={est:.4f} CI[{lo:.4f};{hi:.4f}]')
# drift: availability, archived, price change
prod=pd.read_pickle(D+'/prod.pkl'); prod=prod[prod.account=='oz_acc1'].drop_duplicates('sku')
g=AB[['sku','grp']].drop_duplicates().merge(prod[['sku','offer_id','is_archived','name']],how='left')
P('archived now',g.groupby('grp').is_archived.mean().round(4).to_dict())
av=AB.assign(p=np.select([AB.d<='2026-08-08',AB.d<='2026-08-18',AB.d<='2026-09-01'],['pre1','pre2','post1'],'post2')).groupby(['grp','p']).avail.mean().round(4)
P('availability',av.to_dict())
pr=AB[AB.d.isin([pd.Timestamp('2026-09-01'),pd.Timestamp('2026-09-05'),pd.Timestamp('2026-08-18'),pd.Timestamp('2026-09-13')])].pivot_table(index=['sku','grp'],columns='d',values='price')
pr.columns=[c.strftime('%d%m') for c in pr.columns]
pr['ch']=pr['0509']/pr['0109']-1; pr['ch2']=pr['0109']/pr['1808']-1; pr['ch3']=pr['1309']/pr['0509']-1
for c,lab in [('ch','01.09->05.09'),('ch2','18.08->01.09'),('ch3','05.09->13.09')]:
    s=pr.groupby('grp')[c]
    P(f'price {lab}: median',s.median().round(4).to_dict(),'share>+5%',s.apply(lambda x:(x>0.05).mean()).round(3).to_dict(),'share<-5%',s.apply(lambda x:(x<-0.05).mean()).round(3).to_dict())
# contamination
coh=pickle.load(open(D+'/coh.pkl','rb'))
act=pd.read_pickle(D+'/act.pkl'); act=act[act.account=='oz_acc1']
card=pd.read_pickle(D+'/card.pkl'); card=card[card.account=='oz_acc1']
g['in_action']=g.offer_id.isin(set(act.offer_id)); g['in_card']=g.offer_id.isin(set(card.offer_id))
g['in_card_touched']=g.offer_id.isin(set(card[card.attempts.fillna(0)>0].offer_id))
for k,v in coh.items(): g['c_'+k]=g.sku.isin(v)
P('contamination counts',g.groupby('grp')[[c for c in g.columns if c.startswith('in_') or c.startswith('c_')]].sum().to_dict())
# robustness: exclude contaminated SKUs, DiD orders post1 vs pre2
bad=set(g[g.in_action|g.in_card_touched|g.c_E5_treatment|g.c_E5_control|g.c_E7_treatment].sku)
dd=(posts['post1_20.08-01.09']-pre2).reset_index(); dd=dd[~dd.sku.isin(bad)]
for m in ['orders','rev','net']:
    est,lo,hi,_=boot(dd[dd.grp=='A'][m],dd[dd.grp=='B'][m])
    P(f'clean(excl {len(bad)}) post1 vs pre2 {m}: DiD={est:.4f} CI[{lo:.4f};{hi:.4f}]')
# sales concentration: how many SKUs drive
for w,nm in [(posts['post_20.08-13.09'],'post_all')]:
    x=w.reset_index(); x=x[x.rev>0]
    P(nm,'sku with sales A/B',x.groupby('grp').size().to_dict())
g.to_pickle(D+'/ab_meta.pkl')
open(R+'/s4_out.txt','w').write('\n'.join(out))

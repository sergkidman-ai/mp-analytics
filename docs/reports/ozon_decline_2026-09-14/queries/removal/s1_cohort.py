import pandas as pd, numpy as np
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
D=R+'/data'
b=pd.read_pickle(D+'/bids.pkl'); b=b[b.account=='oz_acc1']
b['captured_at']=pd.to_datetime(b.captured_at)
csv=pd.read_csv('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',dtype=str)
out=[]
def P(*a):
    s=' '.join(str(x) for x in a); out.append(s); print(s)
days=sorted(b.captured_at.dt.date.unique())
P('bids days acc1 since 08-01:',days[0],'..',days[-1],'n',len(days))
def pairs(d): x=b[b.captured_at==pd.Timestamp(d)]; return set(zip(x.campaign_id,x.sku))
p8,p9=pairs('2026-08-08'),pairs('2026-08-09')
rem=p8-p9; rem_sku={s for _,s in rem}
sku8={s for _,s in p8}; sku9={s for _,s in p9}
P('08.08 pairs',len(p8),'sku',len(sku8),'| 09.08 pairs',len(p9),'sku',len(sku9))
P('removed pairs',len(rem),'removed sku (any pair)',len(rem_sku),'sku fully gone',len(sku8-sku9),'new pairs 09.08',len(p9-p8),'new sku',len(sku9-sku8))
P('removed sku still with other pair on 09.08',len(rem_sku&sku9))
cp=set(zip(csv.campaign_id,csv.sku)); P('csv pairs',len(cp),'sku',csv.sku.nunique(),'csv==removed pairs',cp==rem,'inter',len(cp&rem))
g=csv.groupby('sku').group.agg(lambda s:''.join(sorted(set(s))))
P('sku group counts',g.value_counts().to_dict(),'pairs by group',csv.group.value_counts().to_dict())
A=set(g[g=='A'].index); B=set(g[g=='B'].index)
# cohort snapshots compare
for nm,f,grp in [('E8_treatment','A',A),('E8_control','B',B)]:
    c=pd.read_csv(f'/opt/mp-analytics/docs/experiments/cohorts/{nm}_2026-08-20.csv',dtype=str)
    P(nm,'n',c.sku.nunique(),'==csv group',set(c.sku)==grp,'applied',c.applied.value_counts().to_dict())
res={}
for d in ['2026-08-19','2026-08-20','2026-09-01','2026-09-14']:
    x=b[b.captured_at==pd.Timestamp(d)]; s=set(x.sku); pp=set(zip(x.campaign_id,x.sku))
    P(d,'A sku in ads',len(A&s),'/',len(A),'A pairs restored',len(cp_A:=({p for p in cp if p[1] in A}&pp)),
      '| B sku in ads',len(B&s),'B pairs',len({p for p in cp if p[1] in B}&pp),
      '| A sku in other campaign pair only',len({sk for sk in A&s if not any((c_,sk) in pp for c_ in csv[csv.sku==sk].campaign_id)}) if d=='2026-09-14' else '')
# B presence every day after 20.08
bd=b[b.captured_at>=pd.Timestamp('2026-08-20')]
Bd=bd[bd.sku.isin(B)]
P('B sku ever in bids 20.08-14.09:',Bd.sku.nunique(),'days with any B:',Bd.captured_at.nunique())
Ad=bd[bd.sku.isin(A)].groupby('captured_at').sku.nunique()
P('A sku in bids per day min/max',Ad.min(),Ad.max())
# A dropped out between 20.08 and 14.09
s20=set(b[b.captured_at=='2026-08-20'].sku); s14=set(b[b.captured_at=='2026-09-14'].sku)
P('A present 20.08 not 14.09',len((A&s20)-s14),'A present 14.09 not 20.08',len((A&s14)-s20))
# reference: acc1 SKUs in ads on 09.08 and still on 20.08..14.09 (not removed)
ref=(sku9&s20&s14)-rem_sku
P('reference never-removed sku (in bids 09.08, 20.08, 14.09, not in wave1)',len(ref))
pd.Series(sorted(A)).to_csv(D+'/A.csv',index=False); pd.Series(sorted(B)).to_csv(D+'/B.csv',index=False)
pd.Series(sorted(ref)).to_csv(D+'/REF.csv',index=False)
# campaigns of removed pairs
P('removed pairs by campaign',csv.groupby(['campaign_id']).size().to_dict())
open(R+'/s1_out.txt','w').write('\n'.join(out))

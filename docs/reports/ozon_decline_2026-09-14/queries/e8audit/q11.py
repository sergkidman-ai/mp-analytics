import csv,sys,collections,json
import pandas as pd
sys.path.insert(0,'/opt/mp-analytics')
from core import db
R='/opt/mp-analytics/docs/'
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open(R+'reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
A={s for s,g in grp.items() if g=='A'}; B={s for s,g in grp.items() if g=='B'}
out=open(S+'q11_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:350]); out.write(s+'\n')
prod=db.query("select sku::text sku, offer_id from ozon_product where account='oz_acc1'")
s2o={r['sku']:str(r['offer_id']) for r in prod}; psku=set(s2o)
pis={r['s'] for r in db.query("select distinct sku::text s from ozon_price_index where account='oz_acc1' and collected_on='2026-09-01'")}
oA={s2o[s] for s in A}; oB={s2o[s] for s in B}
def space(name,skus):
    skus={str(x) for x in skus}; P(f'{name}: n={len(skus)} in_product_sku={len(skus&psku)} in_priceidx_sku={len(skus&pis)}')
def ovo(name,offers):
    offers={str(x) for x in offers}; P(f'  {name} via offer: A={len(offers&oA)} B={len(offers&oB)}')
C=R+'experiments/cohorts/'
for nm,f in [('E5T','E5_treatment_2026-08-20.csv'),('E5C','E5_control_2026-08-20.csv'),('E7','E7_treatment_2026-08-20.csv'),('HALO_CORE_A','HALO_CORE_A_STABLE_ADVERTISED_2026-08-21.csv'),('HALO_BASE','HALO_BASELINE_NEVER_ADVERTISED_2026-08-21.csv')]:
    x=pd.read_csv(C+f,dtype=str); space(nm,x.sku)
    if 'offer_id' in x: ovo(nm,x.offer_id)
    else: ovo(nm,[s2o.get(s) for s in x.sku if s in s2o])
bn=pd.read_csv(R+'reports/ozon_acc1_bundles_start.csv',dtype=str); space('bundle_sku',bn.sku); space('bundle_base',bn.base)
P('  bundle base sample',list(bn.base[:3]),'sku sample',list(bn.sku[:2]))
ovo('bundle_base_as_offer',bn.base); ovo('bundle_sku_as_offer',bn.sku)
space('steplog E1',[r['s'] for r in db.query("select distinct sku::text s from mkt_ozon_bid_step_log where account='oz_acc1' and step_date between '2026-08-08' and '2026-08-13'")])
pp=db.query("select payload->'products' pr from raw_ozon_posting where account='oz_acc1' and in_process_at>='2026-08-20'")
ps=set(); po=set()
for r in pp:
    pr=r['pr'] or []
    if isinstance(pr,str): pr=json.loads(pr)
    for p in pr: ps.add(str(p.get('sku'))); po.add(str(p.get('offer_id')))
space('posting sku after20',ps); ovo('posting offers after20',po)
P('  posting sku in A/B',len(ps&A),len(ps&B))

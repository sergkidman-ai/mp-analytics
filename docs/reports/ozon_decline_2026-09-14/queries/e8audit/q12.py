import csv,sys,collections
import pandas as pd
sys.path.insert(0,'/opt/mp-analytics')
from core import db
R='/opt/mp-analytics/docs/'
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open(R+'reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
out=open(S+'q12_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:400]); out.write(s+'\n')
prod=db.query("select sku::text sku, offer_id from ozon_product where account='oz_acc1'")
s2o={r['sku']:str(r['offer_id']) for r in prod}
bn=pd.read_csv(R+'reports/ozon_acc1_bundles_start.csv',dtype=str)
bases=set(bn.base)
c=collections.Counter()
for s,g in grp.items():
    o=s2o.get(s,'')
    if o in bases: c[(g,'exact_base')]+=1
    elif o[:4] in bases: c[(g,'child_of_base(4-prefix)')]+=1
P('bundle base relation',sorted(c.items()))
P('campaign titles after 20.08',[(r['c'],r['t'],r['n']) for r in db.query("select campaign_id::text c, max(campaign_title) t, count(distinct sku) n from ozon_bids where account='oz_acc1' and captured_at='2026-09-14' group by 1")])
P('ozon_ads campaigns since 20.08',[(r['c'],r['t'],r['a'],r['sp']) for r in db.query("select campaign_id::text c, max(title) t, max(adv_type) a, round(sum(spend)) sp from ozon_ads where account='oz_acc1' and period>='2026-08-01' group by 1 order by 4 desc")])
P('ozon_ads period sample',[str(r['period']) for r in db.query("select distinct period from ozon_ads where account='oz_acc1' order by 1 desc limit 5")])

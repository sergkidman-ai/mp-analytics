import csv,sys,collections
sys.path.insert(0,'/opt/mp-analytics')
from core import db
R='/opt/mp-analytics/docs/'
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open(R+'reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
A={s for s,g in grp.items() if g=='A'}; B={s for s,g in grp.items() if g=='B'}
out=open(S+'q10_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:350]); out.write(s+'\n')
prod=db.query("select sku::text sku, offer_id from ozon_product where account='oz_acc1'")
s2o={r['sku']:str(r['offer_id']) for r in prod}
P('len sku csv',collections.Counter(len(s) for s in grp).most_common(3))
pi=db.query("select sku::text s, offer_id, collected_on d, price from ozon_price_index where account='oz_acc1' and collected_on between '2026-08-25' and '2026-09-10'")
P('price_index dates',sorted(collections.Counter(str(r['d']) for r in pi).items()))
pis={r['s'] for r in pi}; pio={str(r['offer_id']) for r in pi}
P('price_index sku overlap A/B',len(A&pis),len(B&pis),'offer overlap A/B',len({s2o.get(s) for s in A}&pio),len({s2o.get(s) for s in B}&pio), 'sample sku', list(pis)[:2])
# balance of price change by offer: first date >= 09-01 and <=09-05
byo=collections.defaultdict(dict)
for r in pi: byo[str(r['offer_id'])][str(r['d'])]=float(r['price'] or 0)
ds=sorted({str(r['d']) for r in pi}); d0=max([d for d in ds if d<='2026-09-01'] or [ds[0]]); d1=min([d for d in ds if d>='2026-09-05'] or [ds[-1]])
P('compare',d0,d1)
for g,SS in (('A',A),('B',B)):
    offs=[s2o.get(s) for s in SS]; ok=[o for o in offs if o in byo and d0 in byo[o] and d1 in byo[o] and byo[o][d0]>0]
    up=sum(1 for o in ok if byo[o][d1]>byo[o][d0]*1.05); dn=sum(1 for o in ok if byo[o][d1]<byo[o][d0]*0.95)
    import statistics
    med=statistics.median([byo[o][d1]/byo[o][d0] for o in ok]) if ok else None
    P(g,'obs',len(ok),'up>5%',up,round(100*up/max(len(ok),1),1),'down>5%',dn,'median ratio',round(med,4) if med else None)
j=db.query("select sku::text s, action, applied, week_start, decided_on from mkt_ozon_bid_journal where account='oz_acc1' and decided_on>'2026-08-19' and action not like 'wave1%%'")
c=collections.Counter((grp[r['s']],r['action'],bool(r['applied'])) for r in j if r['s'] in grp)
P('journal after19 on cohort',sorted(c.items()))

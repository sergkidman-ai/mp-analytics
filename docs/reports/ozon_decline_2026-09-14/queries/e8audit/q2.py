import csv,sys,collections,datetime as dt
sys.path.insert(0,'/opt/mp-analytics')
from core import db
F='/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv'
rows=list(csv.DictReader(open(F,encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}; links={(r['campaign_id'],r['sku']):r['group'] for r in rows}
out=open(sys.argv[1],'w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:400]); out.write(s+'\n')
for r in db.query("""select action, week_start, decided_on, applied, count(*) n, min(created_at) c0, max(created_at) c1, min(applied_at) a0, max(applied_at) a1
 from mkt_ozon_bid_journal where account='oz_acc1' and action like 'wave1%%' group by 1,2,3,4 order by 1"""): P('J',dict(r))
j=db.query("select campaign_id::text c, sku::text s, action from mkt_ozon_bid_journal where account='oz_acc1' and action like 'wave1%%'")
jl={(r['c'],r['s']):('A' if r['action']=='wave1_restore' else 'B') for r in j}
P('journal links',len(jl),'in csv',sum(1 for k in jl if k in links),'group agree',sum(1 for k,v in jl.items() if links.get(k)==v),'csv not in journal',sum(1 for k in links if k not in jl))
jj=db.query("select campaign_id::text c, sku::text s, action, week_start from mkt_ozon_bid_journal where account='oz_acc1'")
cur=collections.Counter()
for r in jj:
    k=(r['c'],r['s'])
    if k in links: cur[(links[k],r['action'],str(r['week_start']))]+=1
for k,v in sorted(cur.items()): P('Jcsv',k,v)
for r in db.query("select step_date, applied, count(*) n, min(created_at) c0,max(created_at) c1 from mkt_ozon_bid_step_log where account='oz_acc1' and step_date between '2026-08-17' and '2026-08-21' group by 1,2 order by 1"): P('STEP',dict(r))
days=db.query("select captured_at::date d, count(distinct captured_at) nsnap, min(captured_at) t0, max(captured_at) t1, count(*) n from ozon_bids where account='oz_acc1' and captured_at>='2026-07-20' group by 1 order by 1")
dd=[r['d'] for r in days]
P('snapdays',len(dd),'first',dd[0],'last',dd[-1])
allday=[dd[0]+dt.timedelta(i) for i in range((dd[-1]-dd[0]).days+1)]
P('missing days',[str(x) for x in allday if x not in dd])
P('multi-snap days',[(str(r['d']),r['nsnap']) for r in days if r['nsnap']>1][:20])
P('key snaps',[(str(r['d']),str(r['t0']),str(r['t1']),r['n']) for r in days if str(r['d']) in ('2026-08-08','2026-08-09','2026-08-19','2026-08-20')])

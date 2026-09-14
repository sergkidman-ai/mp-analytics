import csv,sys,collections,json
sys.path.insert(0,'/opt/mp-analytics')
from core import db
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
out=open(S+'q8_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:400]); out.write(s+'\n')
T='2026-08-19 14:45:00+00'
# ads daily pre windows
for r in db.query("""select case when stat_date<='2026-08-08' then 'pre1' when stat_date between '2026-08-10' and '2026-08-18' then 'pre2' else 'other' end w,
  count(*) n, sum((collected_at> %s)::int) late, min(collected_at) c0, max(collected_at) c1
  from mkt_ozon_ads_sku_daily where account='oz_acc1' and stat_date between '2026-07-27' and '2026-08-18' group by 1 order by 1""",(T,)): P('ADS',r['w'],r['n'],'late',r['late'],str(r['c0'])[:16],str(r['c1'])[:16])
# postings
ps=db.query("""select posting_number, status, in_process_at, period_from, period_to, loaded_at, payload->'products' pr from raw_ozon_posting
  where account='oz_acc1' and in_process_at>='2026-07-27' and in_process_at<'2026-08-19'""")
c=collections.defaultdict(lambda: collections.Counter())
for r in ps:
    d=str(r['in_process_at'])[:10]
    w='pre1' if d<='2026-08-08' else ('pre2' if d>='2026-08-10' else 'd0908')
    pr=r['pr'] or []
    if isinstance(pr,str): pr=json.loads(pr)
    gs={grp.get(str(p.get('sku'))) for p in pr}-{None}
    for g in gs or {'other'}:
        k=(w,g); cc=c[k]; cc['n']+=1
        rew = str(r['period_to'])>='2026-08-19'
        cc['loaded_late']+= int(str(r['loaded_at'])>T[:19])
        cc['rewritten_after']+=int(rew)
        canc = r['status']=='cancelled'
        cc['cancel']+=int(canc)
        if not rew: cc['n_norew']+=1; cc['cancel_norew']+=int(canc)
for k in sorted(c):
    x=c[k]; P('POST',k,dict(x), 'cancel%',round(100*x['cancel']/x['n'],2), 'cancel%_norew', round(100*x['cancel_norew']/x['n_norew'],2) if x['n_norew'] else None)
P('period_to distribution pre rows', collections.Counter(str(r['period_to'])[:10] for r in ps).most_common(8))

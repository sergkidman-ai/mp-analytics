import csv,hashlib,collections,sys
sys.path.insert(0,'/opt/mp-analytics')
F='/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv'
rows=list(csv.DictReader(open(F,encoding='utf-8')))
mis=sum(1 for r in rows if ('A' if hashlib.md5(f'20260819:{r["sku"]}'.encode()).digest()[0]%2==0 else 'B')!=r['group'])
g=collections.defaultdict(set); pairs=collections.Counter()
for r in rows: g[r['sku']].add(r['group']); pairs[(r['campaign_id'],r['sku'])]+=1
print('rows',len(rows),'mismatch_hash',mis,'sku',len(g),'sku_both',sum(1 for v in g.values() if len(v)>1),'dup_pairs',sum(1 for v in pairs.values() if v>1))
c=collections.Counter(next(iter(v)) for v in g.values()); print('sku by group',c)
print('links by group',collections.Counter(r['group'] for r in rows))
multi=[s for s in g if sum(1 for r in rows if r['sku']==s)>1] if False else None
from core import db
for t in ['ozon_bids','mkt_ozon_bid_journal','mkt_ozon_bid_step_log','mkt_ozon_ads_sku_daily','raw_ozon_posting']:
    cols=db.query("select column_name from information_schema.columns where table_name=%s order by ordinal_position",(t,))
    print(t,[c['column_name'] for c in cols])

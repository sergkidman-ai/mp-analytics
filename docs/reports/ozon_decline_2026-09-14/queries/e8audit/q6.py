import csv,sys,collections,json
sys.path.insert(0,'/opt/mp-analytics')
from core import db
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
out=open(S+'q6_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:500]); out.write(s+'\n')
P('search_promo status overall', [(r['promo_status'],r['n']) for r in db.query("select promo_status,count(distinct sku) n from ozon_search_promo where account='oz_acc1' and captured_at::date='2026-09-14' group by 1")])
P('raw_ozon_transaction cols',[c['column_name'] for c in db.query("select column_name from information_schema.columns where table_name='raw_ozon_transaction' order by ordinal_position")])

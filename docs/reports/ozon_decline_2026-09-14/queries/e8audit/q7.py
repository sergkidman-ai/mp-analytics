import csv,sys,collections,json
sys.path.insert(0,'/opt/mp-analytics')
from core import db
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
out=open(S+'q7_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:600]); out.write(s+'\n')
q=db.query("""select payload->>'operation_type' ot, payload->>'operation_type_name' otn, count(*) n, round(sum((payload->>'amount')::numeric)) amt
 from raw_ozon_transaction where account='oz_acc1' and (payload->>'operation_date')>='2026-08-01'
 and (payload->>'operation_type_name' ilike '%%продвиж%%' or payload->>'operation_type_name' ilike '%%заказ%%' or payload->>'operation_type' ilike '%%promo%%' or payload->>'operation_type' ilike '%%CostPer%%') group by 1,2 order by n desc""")
for r in q: P('OT',r['ot'],r['otn'],r['n'],r['amt'])
tx=db.query("""select substr(payload->>'operation_date',1,10) d, payload->>'operation_type' ot, payload->'items' items, (payload->>'amount')::numeric amt from raw_ozon_transaction
 where account='oz_acc1' and (payload->>'operation_date')>='2026-07-27' and (payload->>'operation_type' ilike '%%promo%%' or payload->>'operation_type_name' ilike '%%продвиж%%')""")
c=collections.defaultdict(lambda:[0,0.0])
for r in tx:
    items=r['items'] or []
    if isinstance(items,str): items=json.loads(items)
    gs={grp.get(str(i.get('sku'))) for i in items} or {None}
    for g in gs:
        per='pre' if r['d']<'2026-08-09' else ('off' if r['d']<'2026-08-20' else 'post')
        k=(r['ot'],per,g or 'other'); c[k][0]+=1; c[k][1]+=float(r['amt'] or 0)
for k in sorted(c, key=str): P('PROMO',k,c[k][0],round(c[k][1]))

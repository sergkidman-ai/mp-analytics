# поток: ozon-family; read-only attribute/schema and DB coverage reads.
import sys,json,pathlib,datetime,time,collections,fcntl
ROOT=pathlib.Path('/opt/mp-analytics');sys.path.insert(0,str(ROOT))
from core import db
from collectors.ozon import _headers
import requests,psycopg2,psycopg2.extras
O=pathlib.Path(__file__).parent/'snapshot'
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def save(n,x):
 tmp=O/(n+'.tmp');tmp.write_text(json.dumps(x,ensure_ascii=False,default=str));tmp.replace(O/n)
def attrs():
 s=requests.Session();s.headers.update(_headers('oz_acc1'))
 cats=json.loads((O/'catalog.json').read_text())+json.loads((O/'catalog_archived.json').read_text())
 # Only the current category schema is needed; archived categories remain explicit unknowns.
 pairs=[(17028935,95980)]
 for cat,typ in pairs:
  n=f'category_schema_{cat}_{typ}.json'
  if not (O/n).exists():
   r=s.post('https://api-seller.ozon.ru/v1/description-category/attribute',json={'description_category_id':cat,'type_id':typ,'language':'RU'},timeout=90)
   save(n,{'http':r.status_code,'observed_at':now(),'endpoint':'/v1/description-category/attribute','request':{'description_category_id':cat,'type_id':typ,'language':'RU'},'body':r.json()})
 # Exact list of already enumerated product IDs, no second product enumeration.
 ids=sorted({str(x['id']) for x in cats},key=int); allrows=[];prov=[]
 for i in range(0,len(ids),1000):
  n=f'attrs_{i//1000:03}.json'
  if (O/n).exists():z=json.loads((O/n).read_text())
  else:
   req={'filter':{'product_id':ids[i:i+1000],'visibility':'ALL'},'limit':1000,'sort_dir':'ASC'}
   r=s.post('https://api-seller.ozon.ru/v4/product/info/attributes',json=req,timeout=120)
   if r.status_code==429:
    time.sleep(6);r=s.post('https://api-seller.ozon.ru/v4/product/info/attributes',json=req,timeout=120)
   z={'http':r.status_code,'observed_at':now(),'endpoint':'/v4/product/info/attributes','request':req,'body':r.json()};save(n,z)
  if z['http']!=200:print('ATTR FAILED',z['http'],flush=True);break
  rows=z['body'].get('result',[]);allrows+=rows
  prov.extend({'product_id':str(x['id']),'source':'snapshot/'+n,'observed_at':z['observed_at']} for x in rows)
  print('ATTR',i//1000,len(rows),flush=True);time.sleep(.3)
 # Raw attrs_*.json are canonical. Do not duplicate ~424 MB into an aggregate.
 save('attributes_provenance.json',prov)
 got={str(x['id']) for x in allrows}
 save('attribute_coverage.json',{'requested_unique':len(ids),'rows':len(allrows),'unique':len(got),'missing_product_ids':sorted(set(ids)-got),'completed_at':now()})
def coverage():
 conn=psycopg2.connect(db.DATABASE_URL);conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
 schema=json.loads((O/'db_schema.json').read_text());tables=collections.defaultdict(set)
 for x in schema:tables[x['table_name']].add(x['column_name'])
 targets={'raw_ozon_transaction':'period_from','raw_ozon_realization':'year','raw_ozon_accrual':'accrual_date','raw_ozon_orders_report':'delivered_at','raw_ozon_posting':'in_process_at','margin_by_sku':'period_from','mkt_ozon_ads_sku_daily':'stat_date','ozon_ads':'period','ozon_bids':'captured_at','sku_relations':'built_at'}
 result={'observed_at':now(),'isolation':'REPEATABLE READ READ ONLY','tables':{}}
 with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
  for t,d in targets.items():
   if t not in tables:result['tables'][t]={'status':'missing_table'};continue
   where=" WHERE account='oz_acc1'" if 'account' in tables[t] else ''
   if 'platform' in tables[t]:where+=(" AND" if where else " WHERE")+" platform='ozon'"
   q=f'SELECT count(*) n,min({d}) first,max({d}) last FROM {t}'+where
   c.execute(q);res=dict(c.fetchone());result['tables'][t]={'sql':q,**res}
   if t=='sku_relations' and res['n']:
    c.execute('SELECT * FROM sku_relations'+where);save('db_sku_relations.json',[dict(r) for r in c.fetchall()])
 save('db_year_coverage.json',result);conn.rollback();conn.close()
 print(json.dumps(result,default=str))
if __name__=='__main__':
 with (O/'supplement.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  attrs() if sys.argv[-1]=='attrs' else coverage()

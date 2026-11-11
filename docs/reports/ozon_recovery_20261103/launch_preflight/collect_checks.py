# поток: mkt
"""Strict read-only launch preflight, whitelisted endpoints, only oz_acc1."""
import sys,pathlib,json,csv,datetime as dt,time,hashlib,decimal
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
import requests
from collectors import ozon_ads as ads,ozon_bids as bids,ozon as seller
O=pathlib.Path(__file__).parent;ROOT=O.parent;R=O/'raw';R.mkdir(exist_ok=True)
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
orig=requests.sessions.Session.request
PERF=ads.PERF
READPOST={'/api/client/token','/api/client/min/sku','/api/client/campaign/cpc/v2/product/budget/calculate','/api/client/campaign/cpc/v2/product/budget/min','/api/client/campaign/cpc/v2/product/budget','/api/client/statistics','/api/client/statistics/json'}
SELLERPOST={'/v3/product/info/list','/v3/product/list'}
def guard(self,method,url,**kw):
 method=method.upper()
 if url.startswith(PERF):
  path=url[len(PERF):];assert (method=='GET' and (path=='/api/client/campaign' or ('/v2/products' in path) or path=='/api/client/limits/list' or path.startswith('/api/client/statistics'))) or (method=='POST' and path in READPOST), 'Marketplace write forbidden'
 elif url.startswith('https://api-seller.ozon.ru'):
  path=url[len('https://api-seller.ozon.ru'):];assert method=='POST' and path in SELLERPOST,'Seller write forbidden'
 else:raise AssertionError('Unexpected host')
 r=orig(self,method,url,**kw)
 log={'at_utc':now(),'method':method,'path':path,'http':r.status_code}
 if path!='/api/client/token':log['body']=kw.get('json');log['params']=kw.get('params')
 with (O/'read_requests.jsonl').open('a') as f:f.write(json.dumps(log,ensure_ascii=False)+'\n')
 return r
requests.sessions.Session.request=guard
H={'Authorization':'Bearer '+ads._token('oz_acc1')};SH=seller._headers('oz_acc1')
def info(ids,name,purpose):
 p=R/(name+'.json')
 if p.exists():return json.loads(p.read_text())
 r=requests.post(seller.PRODUCT_INFO_URL,headers=SH,json={'sku':ids},timeout=100)
 z={'http':r.status_code,'retrieved_at_utc':now(),'purpose':purpose,'requested_skus':ids,'body':r.json()};save('raw/'+p.name,z)
 return z
if __name__=='__main__':
 source=ROOT/'revised_prepared_composition_2953.csv';digest=hashlib.sha256(source.read_bytes()).hexdigest();assert digest=='fde1af9669d5ca1c0c89c04aaf3cb2d435de7b23a8ada5279c19a644586a38ed'
 with source.open() as f:prepared=list(csv.DictReader(f))
 ids=[r['sku'] for r in prepared];assert len(ids)==len(set(ids))==2953
 save('provenance.json',{'source':str(source.resolve()),'sha256':digest,'source_kind':'recovered preparation-campaign composition on 2026-11-03, new VPS','classification':'owner-confirmed 49173478=new;49173499=bundles;49173502=old','original_immutable_2963_available':False,'original_sha256':'54f332222592045527346f050d331d065c601ec1adec90a19b9b1e9fb95325c2','verified_at_utc':now()})
 for i in range(0,len(ids),500):
  z=info(ids[i:i+500],f'prepared_info_{i//500:02}','fresh stock, visibility, price, model family check of recovered whitelist')
  print('prepared info',i//500,'HTTP',z['http'],'items',len(z['body'].get('items',[])),flush=True);time.sleep(.3)
 p=R/'campaigns.json'
 if not p.exists():
  r=requests.get(PERF+'/api/client/campaign',headers=H,timeout=60);save('raw/'+p.name,{'http':r.status_code,'retrieved_at_utc':now(),'body':r.json()})
 cs=json.loads(p.read_text())['body']['list'];active=set();coverage=[]
 for c in cs:
  if c.get('PaymentType')!='CPC' or c.get('state')!='CAMPAIGN_STATE_RUNNING':continue
  cid=str(c['id']);p=R/f'active_products_{cid}.json'
  if p.exists():z=json.loads(p.read_text())
  else:
   pp,code=bids._campaign_products(H,cid);z={'campaign_id':cid,'http':code,'retrieved_at_utc':now(),'products':pp};save('raw/'+p.name,z)
  active|={str(p['sku']) for p in z['products']};coverage.append({'campaign_id':cid,'http':z['http'],'products':len(z['products'])});print('active composition',cid,z['http'],len(z['products']),flush=True);time.sleep(.2)
 save('active_campaign_coverage.json',coverage)
 # Exact current family overlap requires model metadata of active CPC products only, not full cabinet/year data.
 aid=sorted(active-set(ids))
 for i in range(0,len(aid),500):
  z=info(aid[i:i+500],f'active_info_{i//500:02}','only active CPC SKU model_id to exclude whole families already promoted; no full catalog audit')
  print('active family info',i//500,'HTTP',z['http'],'items',len(z['body'].get('items',[])),flush=True);time.sleep(.3)
 # Check only known additional family members from preserved order metadata, no broad catalog fetch.
 pm={}
 for p in R.glob('prepared_info*json'):
  for it in json.loads(p.read_text())['body'].get('items',[]):pm[str(it['model_info']['model_id'])]=True
 seen=set(ids)|active;extra=set()
 for p in (ROOT/'revision_raw').glob('product_info*json'):
  for it in json.loads(p.read_text()).get('body',{}).get('items',[]):
   if str(it.get('model_info',{}).get('model_id')) in pm and str(it.get('sku')) not in seen:extra.add(str(it['sku']))
 for i in range(0,len(extra),500):
  z=info(sorted(extra)[i:i+500],f'family_extra_info_{i//500:02}','refresh only already-known outside members of recovered whitelist families')
  print('extra relevant family info',i//500,'HTTP',z['http'],'items',len(z['body'].get('items',[])),flush=True)
 save('bounded_universe.json',{'prepared_skus':ids,'active_cpc_skus':sorted(active),'additional_known_family_skus':sorted(extra),'purpose':'whitelist validation and family safety only'})
 print('CHECK FETCH COMPLETE',flush=True)

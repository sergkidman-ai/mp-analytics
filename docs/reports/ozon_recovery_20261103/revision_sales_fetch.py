# поток: mkt
"""Only fills the missing account/SKU noncancelled-order history; no DB writes."""
import sys,pathlib,json,datetime as dt,time,collections
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from collectors.ozon_postings import fetch_postings
from collectors.ozon import _headers,PRODUCT_INFO_URL
import requests
O=pathlib.Path(__file__).parent;R=O/'revision_raw';R.mkdir(exist_ok=True)
def stamp():return dt.datetime.now(dt.timezone.utc).isoformat()
def save(n,v):(R/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
windows=[('baseline','2026-10-02T00:00:00+03:00','2026-10-29T23:59:59.999000+03:00'),('post','2026-10-31T00:00:00+03:00','2026-11-02T23:59:59.999000+03:00')]
allposts=[];coverage=[]
for name,a,b in windows:
 for scheme in ['fbs','fbo']:
  dest=R/f'postings_{name}_{scheme}.json'
  if dest.exists():v=json.loads(dest.read_text())
  else:
   try:
    posts=fetch_postings('oz_acc1',scheme,a,b,limit=100,window_budget=600,lock_timeout=5)
    v={'account':'oz_acc1','scheme':scheme,'from_msk':a,'to_msk':b,'retrieved_at_utc':stamp(),'complete':True,'postings':posts}
   except Exception as e:
    v={'account':'oz_acc1','scheme':scheme,'from_msk':a,'to_msk':b,'retrieved_at_utc':stamp(),'complete':False,'error_type':type(e).__name__,'message':str(e)[:450]}
   save(dest.name,v)
  coverage.append({k:v[k] for k in v if k!='postings'})
  if v.get('complete'):
   allposts+=v['postings'];print(name,scheme,'complete postings',len(v['postings']),flush=True)
  else:print(name,scheme,'FAILED',v.get('error_type'),v.get('message'),flush=True)
  time.sleep(.4)
save('postings_coverage.json',coverage)
print('Posting statuses:',dict(collections.Counter(p.get('status') for p in allposts)),flush=True)
print('Posting fields:',list(allposts[0]) if allposts else [],flush=True)
# Family mapping is a concrete gap: classify every SKU with noncancelled orders by current model_id.
skus=sorted({str(x['sku']) for p in allposts if not str(p.get('status','')).lower().startswith('cancel') for x in p.get('products',[])})
H=_headers('oz_acc1')
for i in range(0,len(skus),500):
 name=f'product_info_sold_{i//500:02}.json'
 if (R/name).exists():continue
 r=requests.post(PRODUCT_INFO_URL,headers=H,json={'sku':skus[i:i+500]},timeout=90)
 save(name,{'http':r.status_code,'retrieved_at_utc':stamp(),'purpose':'current model_id and SKU aliases for every SKU with observed noncancelled account orders','requested_skus':skus[i:i+500],'body':r.json()})
 print('Sold product metadata',i//500,'HTTP',r.status_code,'items',len(r.json().get('items',[])),flush=True)
 time.sleep(.4)
print('SALES FETCH COMPLETE',flush=True)

# поток: mkt
"""Read-only current CPC membership audit against recovered 2953 identifiers."""
import sys,pathlib,json,time,datetime as dt,collections
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
import requests
from collectors import ozon_ads as a,ozon_bids as b
O=pathlib.Path(__file__).parent;R=O/'addendum_raw';R.mkdir(exist_ok=True)
def save(n,x):(R/n).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def stamp():return dt.datetime.now(dt.timezone.utc).isoformat()
H={'Authorization':'Bearer '+a._token('oz_acc1')}
p=R/'campaigns_current.json'
if not p.exists():
 r=requests.get(a.PERF+'/api/client/campaign',headers=H,timeout=60);r.raise_for_status()
 save(p.name,{'account':'oz_acc1','retrieved_at_utc':stamp(),'http':r.status_code,'purpose':'current payment type/state for prepared-identifier membership audit','body':r.json()})
cs=json.loads(p.read_text())['body']['list'];coverage=[]
for c in cs:
 if c.get('PaymentType')!='CPC':continue
 cid=str(c['id']);p=R/f'products_current_{cid}.json'
 if p.exists():v=json.loads(p.read_text())
 elif c.get('advObjectType')!='SKU':
  v={'campaign_id':cid,'state':c['state'],'type':c['advObjectType'],'complete':False,'reason':'non-SKU CPC campaign; product endpoint not applicable; separately disclosed'}
 elif c['state']=='CAMPAIGN_STATE_ARCHIVED':
  # Existing exact API errors say these compositions cannot be read; do not repeat or unarchive.
  previous=O/f'raw/products_error_{cid}.json'
  v={'campaign_id':cid,'state':c['state'],'type':c['advObjectType'],'complete':False,'reason':'current state remains archived; previously verified HTTP 400 product endpoint','previous_error_source':str(previous.relative_to(O)) if previous.exists() else None}
 else:
  ps,code=b._campaign_products(H,cid)
  v={'campaign_id':cid,'state':c['state'],'type':c['advObjectType'],'retrieved_at_utc':stamp(),'http':code,'complete':code==200,'products':ps,'purpose':'refresh required to classify entire recovered list by current campaign state'}
 save(p.name,v)
 coverage.append({k:x for k,x in v.items() if k!='products'})
 print(cid,c['state'],c['advObjectType'],'complete',v['complete'],'rows',len(v.get('products',[])),flush=True)
 time.sleep(.2)
save('coverage_current.json',coverage)
print('CURRENT AUDIT COMPLETE',flush=True)

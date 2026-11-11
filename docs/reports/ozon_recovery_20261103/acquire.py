# поток: mkt
"""One-off read-only exports via the project's Performance clients, no DB writes."""
import sys,pathlib,json,datetime as dt,time,hashlib,re
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
import requests,gzip
from collectors import ozon_ads as ads,ozon_bids as bids
OUT=pathlib.Path(__file__).parent
RAW=OUT/'raw';RAW.mkdir(exist_ok=True)
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def save(n,v): (OUT/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
# Deny every write endpoint except obtaining a token and requesting a statistics report.
_original=requests.sessions.Session.request
def guarded(self,method,url,**kw):
 method=method.upper()
 assert url.startswith(ads.PERF+'/api/client/'), 'Unexpected host'
 path=url[len(ads.PERF):]
 assert method=='GET' or (method=='POST' and path in ['/api/client/token','/api/client/statistics/json','/api/client/statistics']), 'Mutation forbidden'
 start=now()
 try:
  response=_original(self,method,url,**kw)
  entry={'started_utc':start,'finished_utc':now(),'method':method,'path':path,'http':response.status_code}
  if path!='/api/client/token':entry.update(params=kw.get('params'),body=kw.get('json'))
  with (OUT/'api_requests.jsonl').open('a') as f:f.write(json.dumps(entry,ensure_ascii=False)+'\n')
  return response
 except requests.RequestException as e:
  with (OUT/'api_requests.jsonl').open('a') as f:f.write(json.dumps({'started_utc':start,'method':method,'path':path,'error_type':type(e).__name__})+'\n')
  raise
requests.sessions.Session.request=guarded
H={'Authorization':'Bearer '+ads._token('oz_acc1')}
cs=json.loads((RAW/'campaigns.json').read_text())['list']
SKUS=set('1510947813 863386191 1719404303 1277712843 930627436 2909045114 930623456 1658970308 866442961 1999321262 3557209622 906874407 4504239311 4518308193 652626682 4463532700 4480826404 4518304506 4489633315 4518302691'.split())
def get(path,params,name):
 r=requests.get(ads.PERF+path,headers=H,params=params,timeout=120)
 try:body=r.json()
 except ValueError:
  if r.content[:2]==b'\x1f\x8b':
   (RAW/(name+'.response.gz')).write_bytes(r.content)
   body=json.loads(gzip.decompress(r.content))
  else:
   (RAW/(name+'.response.bin')).write_bytes(r.content)
   body={'text':r.text}
 save('raw/'+name,{'retrieved_at_utc':now(),'http':r.status_code,'body':body})
 return r.status_code,body
if __name__=='__main__':
 products=[]
 for c in cs:
  if c.get('advObjectType')!='SKU':continue
  cid=str(c['id']);dest=RAW/f'products_{cid}.json'
  if dest.exists():
   z=json.loads(dest.read_text());ps=z['products'];code=z['http']
  else:
   ps,code=bids._campaign_products(H,cid)
   save(f'raw/products_{cid}.json',{'retrieved_at_utc':now(),'endpoint':f'GET /api/client/campaign/{cid}/v2/products','http':code,'products':ps,'complete':code==200})
  hit=[str(x.get('sku')) for x in ps if str(x.get('sku')) in SKUS]
  products.append({'campaign_id':cid,'http':code,'products':len(ps),'target_skus':hit})
  print('products',cid,'HTTP',code,'rows',len(ps),'target',len(hit),flush=True)
  time.sleep(.2)
 save('product_coverage.json',products)
 ids=[str(c['id']) for c in cs]
 for a,b in [('2026-10-02','2026-10-31'),('2026-11-01','2026-11-03')]:
  for i in range(0,len(ids),10):
   name=f'daily_{a}_{b}_{i//10:02}.json'
   if (RAW/name).exists():continue
   code,body=get('/api/client/statistics/daily/json',{'campaignIds':ids[i:i+10],'dateFrom':a,'dateTo':b},name)
   print('daily',a,b,i//10,'HTTP',code,'rows',len(body.get('rows') or []),flush=True)
   time.sleep(.3)
 # First report tests actual day grouping. No aggregation is inferred from request alone.
 eligible=[str(c['id']) for c in cs if c.get('advObjectType')=='SKU']
 body={'campaigns':eligible[:10],'dateFrom':'2026-10-02','dateTo':'2026-10-31','groupBy':'DATE'}
 r=requests.post(ads.PERF+'/api/client/statistics/json',headers=H,json=body,timeout=120)
 result=r.json();save('report_probe_request.json',{'request':body,'http':r.status_code,'response':result,'requested_at_utc':now()})
 print('report_probe','HTTP',r.status_code,'fields',list(result),flush=True)

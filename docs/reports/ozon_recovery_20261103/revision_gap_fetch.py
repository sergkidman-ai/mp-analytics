# поток: mkt
"""Only resolves the two explicitly missing SKU-days and refreshes current candidates."""
import sys,pathlib,json,time,datetime as dt,zipfile,io,collections
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
import requests
from collectors import ozon_ads as ads,ozon_bids as bids
O=pathlib.Path(__file__).parent;R=O/'revision_raw';R.mkdir(exist_ok=True)
def stamp():return dt.datetime.now(dt.timezone.utc).isoformat()
def save(n,v):(R/n).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
H={'Authorization':'Bearer '+ads._token('oz_acc1')}
p=R/'campaigns_refresh.json'
if not p.exists():
 r=requests.get(ads.PERF+'/api/client/campaign',headers=H,timeout=60)
 save(p.name,{'http':r.status_code,'retrieved_at_utc':stamp(),'purpose':'current status and discovery of other justified wave candidates','body':r.json()})
cs=json.loads(p.read_text())['body'].get('list',[])
# New CPC creations in the permitted Nov 1-3 interval, plus explicitly identified preparation IDs.
from zoneinfo import ZoneInfo
ids=[]
for c in cs:
 day=dt.datetime.fromisoformat(c['createdAt'].replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
 if c.get('PaymentType')=='CPC' and '2026-11-01'<=day<='2026-11-03':ids.append(str(c['id']))
for cid in ids:
 p=R/f'products_refresh_{cid}.json'
 if p.exists():continue
 ps,code=bids._campaign_products(H,cid)
 save(p.name,{'http':code,'retrieved_at_utc':stamp(),'purpose':'current actual composition, not historical launch composition','products':ps})
 print('Current candidate/preparation composition',cid,'HTTP',code,'rows',len(ps),flush=True)
for sku,day,cids in [('1510947813','2026-10-29',['10659516','12704286','37588017']),('4489633315','2026-10-02',['10604674','12704286'])]:
 stem=f'gap_{sku}_{day}'
 req=R/(stem+'_request.json')
 if (R/(stem+'.zip')).exists() or (R/(stem+'.csv')).exists():continue
 if req.exists():job=json.loads(req.read_text())
 else:
  body={'campaigns':cids,'dateFrom':day,'dateTo':day,'groupBy':'DATE'}
  for attempt in range(5):
   r=requests.post(ads.PERF+'/api/client/statistics',headers=H,json=body,timeout=120)
   if r.status_code!=429:break
   print(stem,'rate_limited',attempt,flush=True);time.sleep(15*(attempt+1))
  job={'request':body,'http':r.status_code,'response':r.json(),'purpose':'verify concrete missing SKU-day; absent row will not become zero','requested_at_utc':stamp()};save(req.name,job)
 if job['http']!=200:print(stem,'request_failed',job['http'],flush=True);continue
 u=job['response']['UUID'];start=time.monotonic()
 while time.monotonic()-start<1800:
  r=requests.get(ads.PERF+'/api/client/statistics/'+u,headers=H,timeout=60);status=r.json();save(stem+'_status.json',{'http':r.status_code,'body':status})
  state=status.get('state')
  if state=='OK':
   r=requests.get(ads.PERF+'/api/client/statistics/report',headers=H,params={'UUID':u},timeout=120)
   ext='.zip' if r.content[:2]==b'PK' else '.csv';(R/(stem+ext)).write_bytes(r.content)
   if ext=='.zip':
    folder=R/stem;folder.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
     for n in z.namelist():
      if not n.endswith('/'):(folder/pathlib.Path(n).name).write_bytes(z.read(n))
   print(stem,'report downloaded HTTP',r.status_code,flush=True);break
  if state in ['ERROR','CANCELLED']:print(stem,'failed',state,flush=True);break
  print(stem,'waiting',state,flush=True);time.sleep(10)
print('GAP FETCH COMPLETE',flush=True)

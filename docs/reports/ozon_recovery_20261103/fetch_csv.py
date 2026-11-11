# поток: mkt
"""Read-only CSV reports to preserve explicit zero values omitted by JSON encoding."""
from acquire import *
import zipfile,io
coverage=json.loads((OUT/'product_coverage.json').read_text())
ids=[r['campaign_id'] for r in coverage if r['target_skus']]
for a,b in [('2026-10-02','2026-10-31'),('2026-11-01','2026-11-03')]:
 name=f'csv_{a}_{b}'
 dest=OUT/(name+'_request.json')
 if (RAW/(name+'.zip')).exists() or (RAW/(name+'.csv')).exists():continue
 if dest.exists():job=json.loads(dest.read_text())
 else:
  body={'campaigns':ids,'dateFrom':a,'dateTo':b,'groupBy':'DATE'}
  for attempt in range(5):
   r=requests.post(ads.PERF+'/api/client/statistics',headers=H,json=body,timeout=120)
   if r.status_code!=429:break
   print(name,'rate_limited',attempt,flush=True);time.sleep(15*(attempt+1))
  job={'request':body,'http':r.status_code,'response':r.json(),'requested_at_utc':now()}
  save(name+'_request.json',job)
 if job['http']!=200:
  print(name,'request_failed',job['http'],str(job['response'])[:200],flush=True);continue
 u=job['response'].get('UUID')
 start=time.monotonic()
 while time.monotonic()-start<1800:
  code,status=get('/api/client/statistics/'+u,None,name+'_status.json')
  st=status.get('state')
  if st=='OK':
   r=requests.get(ads.PERF+'/api/client/statistics/report',headers=H,params={'UUID':u},timeout=180)
   ext='.zip' if r.content[:2]==b'PK' else '.csv'
   (RAW/(name+ext)).write_bytes(r.content)
   if ext=='.zip':
    folder=RAW/name;folder.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
     for n in z.namelist():
      if not n.endswith('/'):(folder/pathlib.Path(n).name).write_bytes(z.read(n))
    print(name,'HTTP',r.status_code,'archive files',len(list(folder.iterdir())),flush=True)
   else:print(name,'HTTP',r.status_code,'bytes',len(r.content),flush=True)
   break
  if st in ['ERROR','CANCELLED']:
   print(name,'terminal',st,flush=True);break
  print(name,'waiting',st,flush=True);time.sleep(10)
print('CSV COMPLETE',flush=True)

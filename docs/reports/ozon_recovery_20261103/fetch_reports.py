# поток: mkt
"""Read-only asynchronous reports; DATE grouping is confirmed by returned row fields."""
from acquire import *
def retrieve(job,name):
 u=job['response'].get('UUID') or job['response'].get('uuid')
 if not u:return None
 start=time.monotonic();state=None
 while time.monotonic()-start<1800:
  code,status=get('/api/client/statistics/'+u,None,name+'_status.json')
  state=status.get('state')
  if state=='OK':
   code,body=get('/api/client/statistics/report',{'UUID':u},name+'_result.json')
   print(name,'ready HTTP',code,'blocks',len(body),flush=True)
   return body
  if state in ['ERROR','CANCELLED']:
   print(name,'terminal',state,flush=True);return None
  print(name,'waiting',state,flush=True);time.sleep(10)
 print(name,'queue_timeout',state,flush=True);return None
eligible=[str(c['id']) for c in cs if c.get('advObjectType')=='SKU']
for a,b in [('2026-10-02','2026-10-31'),('2026-11-01','2026-11-03')]:
 for i in range(0,len(eligible),10):
  name=f'sku_report_{a}_{b}_{i//10:02}'
  if (RAW/(name+'_result.json')).exists():continue
  dest=OUT/(name+'_request.json')
  if a=='2026-10-02' and i==0:
   job=json.loads((OUT/'report_probe_request.json').read_text())
   save(name+'_request.json',job)
   if (RAW/'report_probe_result.json').exists():
    save('raw/'+name+'_result.json',json.loads((RAW/'report_probe_result.json').read_text()))
    continue
  elif dest.exists():job=json.loads(dest.read_text())
  else:
   body={'campaigns':eligible[i:i+10],'dateFrom':a,'dateTo':b,'groupBy':'DATE'}
   for attempt in range(5):
    r=requests.post(ads.PERF+'/api/client/statistics/json',headers=H,json=body,timeout=120)
    if r.status_code!=429:break
    print(name,'rate_limited',attempt,flush=True);time.sleep(15*(attempt+1))
   job={'request':body,'http':r.status_code,'response':r.json(),'requested_at_utc':now()}
   save(name+'_request.json',job)
  if job['http']!=200:
   print(name,'request_failed',job['http'],flush=True);continue
  retrieve(job,name)
  time.sleep(1)
print('REPORTS COMPLETE',flush=True)

# поток: mkt
"""Specific 2-Nov shipping/cancellation gap and refreshed 20-SKU ad snapshot only."""
import sys,pathlib,json,datetime as dt,time,zipfile,io,argparse
from zoneinfo import ZoneInfo
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from collectors.ozon_postings import fetch_postings
from collectors.ozon_ads import _token,PERF
import requests
P=pathlib.Path(__file__).parent
parser=argparse.ArgumentParser(description='Existing fixed-20 monitor; refresh a bounded post window, never bids or DB.')
parser.add_argument('--through',type=dt.date.fromisoformat,help='Last complete Moscow day; enables dated refresh mode')
parser.add_argument('--refresh-from',type=dt.date.fromisoformat,default=dt.date(2026,10,31))
parser.add_argument('--output',type=pathlib.Path,help='Separate daily snapshot directory, required with --through')
parser.add_argument('--patience',type=int,default=1800)
a=parser.parse_args()
if a.through:
 if not a.output or not dt.date(2026,10,31)<=a.refresh_from<=a.through<dt.datetime.now(ZoneInfo('Europe/Moscow')).date():
  parser.error('--through requires --output and a complete bounded post-period from 2026-10-31')
 start=a.refresh_from.isoformat();end=a.through.isoformat();adend=end
 suffix=start.replace('-','')+'_'+end.replace('-','')
else:start=end='2026-11-02';adend='2026-11-03';suffix='20261102_03'
O=a.output.resolve() if a.output else P;R=O/'raw';R.mkdir(parents=True,exist_ok=True)
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
original=requests.sessions.Session.request
def guarded(self,method,url,**kw):
 method=method.upper()
 allowed=(method=='GET' and url.startswith(PERF+'/api/client/statistics')) or (method=='POST' and url in [PERF+'/api/client/token',PERF+'/api/client/statistics','https://api-seller.ozon.ru/v4/posting/fbs/list','https://api-seller.ozon.ru/v3/posting/fbo/list'])
 assert allowed,'Monitoring endpoint allowlist: marketplace mutation forbidden'
 r=original(self,method,url,**kw)
 log={'at_utc':now(),'method':method,'path':url.split('.ru',1)[-1],'http':r.status_code}
 if not url.endswith('/token'):log.update(body=kw.get('json'),params=kw.get('params'))
 with (O/'api_reads.jsonl').open('a') as f:f.write(json.dumps(log,ensure_ascii=False)+'\n')
 return r
requests.sessions.Session.request=guarded
save('monitor_config.json',{'account':'oz_acc1','refresh_from_msk':start,'refresh_through_msk':end,'ad_through_msk':adend,'report_suffix':suffix,'fixed_campaign_ids':['37588017','12704286','10659516','10651636','10626733','10604674'],'created_at_utc':now(),'database_writes':0,'marketplace_mutations':0})
for scheme in ['fbs','fbo']:
 p=R/(f'monitor_postings_{start.replace("-","")}_{end.replace("-","")}_{scheme}.json' if a.through else f'monitor_postings_20261102_{scheme}.json')
 if p.exists() and json.loads(p.read_text()).get('complete'):continue
 try:
  posts=fetch_postings('oz_acc1',scheme,start+'T00:00:00+03:00',end+'T23:59:59.999000+03:00',limit=100,window_budget=300)
  z={'account':'oz_acc1','scheme':scheme,'from_msk':start,'through_msk':end,'complete':True,'retrieved_at_utc':now(),'purpose':'fixed-20 post-window refresh including late orders and cancellations; no baseline recollection','postings':posts}
 except Exception as e:z={'account':'oz_acc1','scheme':scheme,'complete':False,'retrieved_at_utc':now(),'error_type':type(e).__name__,'message':str(e)[:300]}
 save('raw/'+p.name,z);print('Refreshed postings',start,end,scheme,z.get('complete'),'rows',len(z.get('postings',[])),flush=True)
# Narrow refreshed 2-3 November campaign/day report for only the six recorded target placements.
H={'Authorization':'Bearer '+_token('oz_acc1')}
ids=['37588017','12704286','10659516','10651636','10626733','10604674']
p=R/'monitor20_report_request.json'
if not p.exists():
 body={'campaigns':ids,'dateFrom':start,'dateTo':adend,'groupBy':'DATE'}
 r=requests.post(PERF+'/api/client/statistics',headers=H,json=body,timeout=120)
 save('raw/'+p.name,{'request':body,'http':r.status_code,'requested_at_utc':now(),'response':r.json(),'purpose':'fixed-20 bounded post-window attribution refresh, no bid changes'})
z=json.loads(p.read_text())
if z['http']==200 and not (R/f'monitor20_{suffix}.zip').exists():
 u=z['response']['UUID'];wait_started=time.monotonic()
 while time.monotonic()-wait_started<a.patience:
  r=requests.get(PERF+'/api/client/statistics/'+u,headers=H,timeout=60);st=r.json();save('raw/monitor20_report_status.json',{'http':r.status_code,'body':st,'checked_at_utc':now()})
  if st.get('state')=='OK':
   rr=requests.get(PERF+'/api/client/statistics/report',headers=H,params={'UUID':u},timeout=180)
   rr.raise_for_status()
   (R/f'monitor20_{suffix}.zip').write_bytes(rr.content);folder=R/f'monitor20_{suffix}';folder.mkdir(exist_ok=True)
   with zipfile.ZipFile(io.BytesIO(rr.content)) as archive:
    for name in archive.namelist():
     if not name.endswith('/'):(folder/pathlib.Path(name).name).write_bytes(archive.read(name))
   print('20 SKU refreshed report ready HTTP',rr.status_code,flush=True);break
  if st.get('state') in ['ERROR','CANCELLED']:print('20SKU report failed',st.get('state'),flush=True);break
  print('20SKU report waiting',st.get('state'),flush=True);time.sleep(10)
complete=z['http']==200 and (R/f'monitor20_{suffix}.zip').exists() and all(json.loads(p.read_text()).get('complete') for p in R.glob('monitor_postings_*.json'))
print('MONITOR FETCH COMPLETE' if complete else 'MONITOR FETCH INCOMPLETE',flush=True)
if not complete:sys.exit(1)

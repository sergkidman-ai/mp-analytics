# поток: mkt
import sys,json,datetime,pathlib
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
OUT=pathlib.Path('/root/ozon_recovery_20261103')
def save(n,v): (OUT/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str))
try:
 from collectors import ozon_ads as ads
 import requests
 h={'Authorization':'Bearer '+ads._token('oz_acc1')}
 r=requests.get(ads.PERF+'/api/client/campaign',headers=h,timeout=60)
 save('raw/campaigns.json',{'status':r.status_code,'body':r.json(),'retrieved_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()})
 cs=r.json().get('list',[])
 print('campaigns_http',r.status_code,'count',len(cs))
 print('fields',list(cs[0]) if cs else [])
 from collections import Counter
 print('types_states',dict(Counter((c.get('advObjectType','')+' / '+c.get('state','')) for c in cs)))
 for c in cs:
  if any('2026-11-0' in str(v) for v in c.values()):
   print('recent',json.dumps(c,ensure_ascii=False)[:1100])
except Exception as e:
 save('api_probe_error.json',{'error_type':type(e).__name__,'message':str(e)[:400]})
 print('api_probe_error',type(e).__name__)
try:
 from core import db
 with db.get_conn() as conn:
  conn.set_session(readonly=True)
  with conn.cursor() as cur:
   cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND (table_name LIKE '%ozon%' OR table_name LIKE '%family%') ORDER BY 1")
   names=[r[0] for r in cur.fetchall()]
 save('db_tables.json',names)
 print('db_tables',', '.join(names))
except Exception as e:
 save('db_probe_error.json',{'error_type':type(e).__name__,'message':str(e)[:400]})
 print('db_probe_error',type(e).__name__)

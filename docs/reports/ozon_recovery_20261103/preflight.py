# поток: mkt
import sys,pathlib,json,datetime,socket,uuid,concurrent.futures,os
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
import requests
from collectors import ozon_ads as ads,ozon as seller
from core import db
OUT=pathlib.Path(__file__).parent
r={'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'hostname':socket.gethostname(),'account':'oz_acc1','permission_mode':'danger-full-access; network enabled; approval never','database_writes':0,'marketplace_mutations':0}
def err(e): return {'error_type':type(e).__name__,'message':str(e)[:500]}
def writecheck():
 p=OUT/('.write_test_'+uuid.uuid4().hex)
 try:
  p.write_text('access test');assert p.read_text()=='access test';p.unlink();return {'create':True,'read':True,'delete':True}
 except Exception as e:return err(e)
def perf():
 out={'credentials_present':ads.has_creds('oz_acc1')}
 try:
  socket.getaddrinfo('api-performance.ozon.ru',443);out['dns']='OK'
  token=ads._token('oz_acc1');out['token_received']=True
  response=requests.get(ads.PERF+'/api/client/campaign',headers={'Authorization':'Bearer '+token},timeout=60)
  out['campaign_http']=response.status_code
  if response.status_code==200:
   (OUT/'raw/campaigns.json').write_text(json.dumps(response.json(),ensure_ascii=False,indent=2))
   out['campaigns']=len(response.json().get('list',[]))
 except Exception as e:out.update(err(e))
 return out
def sell():
 try:
  response=requests.post('https://api-seller.ozon.ru/v3/product/list',headers=seller._headers('oz_acc1'),json={'filter':{'visibility':'ALL'},'limit':1},timeout=60)
  return {'endpoint':'POST /v3/product/list (read-only)','http':response.status_code,'response_received':True}
 except Exception as e:return err(e)
def database():
 try:
  with db.get_conn() as conn:
   conn.set_session(readonly=True)
   with conn.cursor() as c:
    c.execute('SELECT 1');value=c.fetchone()[0]
  return {'query':'SELECT 1','value':value,'readonly':True}
 except Exception as e:return err(e)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 fs={n:pool.submit(fn) for n,fn in [('filesystem',writecheck),('performance_api',perf),('seller_api',sell),('database',database)]}
 for n,f in fs.items():r[n]=f.result()
(OUT/'preflight.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
for n in fs:print(n,json.dumps(r[n],ensure_ascii=False))

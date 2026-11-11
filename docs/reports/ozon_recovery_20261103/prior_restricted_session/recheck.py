# поток: mkt
"""Read-only access recheck for oz_acc1; never prints or persists credentials."""
import datetime,json,pathlib,socket,sys
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from collectors import ozon_ads as ads
from dotenv import dotenv_values
import requests
out=pathlib.Path('/root/ozon_recovery_20261103')
r={'account':'oz_acc1','checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'runtime_has_performance_credentials':ads.has_creds('oz_acc1'),'token_response_received':False,'authorization_verified':False,'campaign_response_received':False,'statistics_requests_sent':0,'marketplace_mutations':0}
v=dotenv_values('/opt/mp-analytics/.env')
r.update({'env_file_has_performance_client_id':bool(v.get('OZON_PERF_CLIENT_ID_ACC1')),'env_file_has_performance_secret':bool(v.get('OZON_PERF_SECRET_ACC1'))})
try:
 socket.getaddrinfo('api-performance.ozon.ru',443)
 r['dns']='OK'
except OSError as e:
 r.update(dns='failed',dns_error_type=type(e).__name__,dns_errno=e.errno)
if r['runtime_has_performance_credentials']:
 try:
  token=ads._token('oz_acc1');r['token_response_received']=True
  response=requests.get(ads.PERF+'/api/client/campaign',headers={'Authorization':'Bearer '+token},timeout=60)
  r.update(campaign_response_received=True,campaign_http_status=response.status_code,authorization_verified=response.status_code==200)
  if response.status_code==200:
   (out/'raw/campaigns.json').write_text(json.dumps(response.json(),ensure_ascii=False,indent=2))
 except requests.HTTPError as e: r.update(token_http_status=e.response.status_code)
 except requests.RequestException as e: r.update(token_error_type=type(e).__name__)
try:
 from core import db
 with db.get_conn() as conn:
  conn.set_session(readonly=True)
  with conn.cursor() as cur:
   cur.execute('SELECT 1');cur.fetchone()
 r['database']='read-only connection OK'
except Exception as e: r.update(database='unavailable',database_error_type=type(e).__name__)
p=pathlib.Path('/opt/mp-analytics/docs/reports/ozon_recovery_20261103')
try:
 p.mkdir(exist_ok=True);r['requested_directory']='exists or created; write access not yet verified'
except OSError as e: r.update(requested_directory='unavailable',directory_error=e.strerror)
(out/'access_recheck.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
print('Credentials configured:',r['runtime_has_performance_credentials'])
print('DNS:',r['dns'],'; token response:',r['token_response_received'])
print('Database:',r['database'])
print('Requested report directory:',r['requested_directory'])

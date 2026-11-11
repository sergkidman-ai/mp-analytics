# поток: ozon-family-strategy. Seven named current gaps, read-only TC mix_data.
import sys,os,datetime,time
from common import *
from dotenv import load_dotenv
import requests
load_dotenv('/opt/mp-analytics/.env')
CODES=('2393','2128','6112','3257','3780','66281','66391')
out=O/'targeted_reads';out.mkdir(exist_ok=True)
cat,cards,_=current();offers={c['offer_id'] for c in cards.values()};assert set(CODES)<=offers
session=requests.Session();key=os.getenv('CARTRIDGE_API_KEY')
if not key:raise RuntimeError('Existing TC credential unavailable; no secret output')
url=os.getenv('CARTRIDGE_API_URL','https://thecartridge.ru/api/catalog/mix_data')
for code in CODES:
 p=out/(code+'.json')
 if p.exists() and read(p).get('http') is not None:continue
 try:r=session.post(url,headers={'Api-Key':key},json={'external_code':code},timeout=25)
 except requests.RequestException as exc:
  save('targeted_read_status.json',{'status':'network_unavailable','exception_type':type(exc).__name__,'code':code,'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
  print('Network read unavailable',type(exc).__name__,code);sys.exit(3)
 try:body=r.json()
 except ValueError:body={'unparseable_response':True}
 z={'external_code':code,'endpoint':'TC /api/catalog/mix_data','request':{'external_code':code},'http':r.status_code,'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'body':body}
 p.write_text(json.dumps(z,ensure_ascii=False,indent=2));print(code,r.status_code,'list_components='+str(len(body)) if isinstance(body,list) else 'response_keys='+','.join(body.keys()),flush=True)
 if r.status_code==429:break
 time.sleep(1.1)
save('targeted_read_status.json',{'status':'completed_once_no_broad_collection','requested_codes':list(CODES),'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})

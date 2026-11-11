# поток: mkt
"""Only the individual minima omitted by excessive old family gates; no full recollection."""
import sys,pathlib,json,time,datetime as dt,decimal,collections
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from collectors.ozon_ads import _token,PERF
import requests
O=pathlib.Path(__file__).parent;PRE=O.parent;D=decimal.Decimal
rows=json.loads((O/'corrected_eligibility.json').read_text());ids=[r['sku'] for r in rows if r['individually_eligible'] and not r['existing_floor']]
assert len(ids)==1466
h={'Authorization':'Bearer '+_token('oz_acc1')}
for i in range(0,len(ids),100):
 p=O/'raw'/f'missing_floor_batch_{i//100:03}.json'
 if p.exists():continue
 body={'sku':ids[i:i+100],'paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU'}
 for attempt in range(5):
  r=requests.post(PERF+'/api/client/min/sku',headers=h,json=body,timeout=90)
  if r.status_code!=429:break
  time.sleep(3*(attempt+1))
 z={'request':body,'http':r.status_code,'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'response':r.json(),'purpose':'only missing individual floors for 1466 restored candidates; 1487 old floors not repeated'}
 p.write_text(json.dumps(z,ensure_ascii=False,indent=2)+'\n')
 print('Missing floor batch',i//100,'HTTP',r.status_code,'records',len(z['response'].get('minBids',[])),flush=True);time.sleep(.25)
floors={};errors={}
for r in rows:
 if r['existing_floor']:
  x=dict(r['existing_floor']);x['check_kind']='reused prior result; refresh before apply';x['source']='../'+x['source'];floors[r['sku']]=x
for p in sorted((O/'raw').glob('missing_floor_batch_*.json')):
 z=json.loads(p.read_text());hits=collections.defaultdict(list)
 for r in z['response'].get('minBids',[]):hits[str(r.get('sku'))].append(r)
 for s in z['request']['sku']:
  hh=hits[s]
  if z['http']!=200 or len(hh)!=1 or isinstance(hh[0].get('bid'),bool) or not isinstance(hh[0].get('bid'),(int,float)):
   errors[s]={'http':z['http'],'error':'missing/non-numeric/non-unique individual minimum','source':str(p.relative_to(O))};continue
  bid=D(str(hh[0]['bid']))
  if not bid.is_finite() or bid<=0 or bid*1000000!=(bid*1000000).to_integral_value():
   errors[s]={'error':'nonpositive/nonfinite/nonexact minimum','source':str(p.relative_to(O))};continue
  floors[s]={'sku':s,'bid_api':hh[0]['bid'],'bid_rub':str(bid),'bid_micro_rub':str(int(bid*1000000)),'api_unit':'RUB per click','product_write_unit':'one millionth RUB','source':str(p.relative_to(O)),'checked_at_utc':z['checked_at_utc'],'paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU','individual_response_record':hh[0],'check_kind':'new missing-result check; refresh before apply if stale'}
(O/'individual_floors_final.json').write_text(json.dumps(list(floors.values()),ensure_ascii=False,indent=2)+'\n')
(O/'floor_errors_final.json').write_text(json.dumps(errors,ensure_ascii=False,indent=2)+'\n')
print('Final individual floor records',len(floors),'errors',len(errors),flush=True)

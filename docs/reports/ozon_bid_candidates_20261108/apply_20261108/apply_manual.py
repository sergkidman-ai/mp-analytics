# поток: mkt / ozon
"""Exact two owner-authorized bid reductions. Autocreated bonus campaigns cannot be written."""
import argparse,csv,datetime as dt,json,sys
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,'/opt/mp-analytics')
import requests
from collectors.ozon_ads import _token,PERF
from collectors.ozon import _headers,PRODUCT_INFO_URL
ROOT=Path('/opt/mp-analytics');P=Path(__file__).parent;O=P.parent;L=O.parent/'ozon_launch_20261106'
p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true');args=p.parse_args()
TARGETS=[{'article':'4838','sku':'1261464375','cid':'10659516','page':29,'before':'106.29','target':'95.66'}, {'article':'2306','sku':'863386567','cid':'10659294','page':21,'before':'50.17','target':'45.15'}]
AUTO={'43607968','44498580','48489446'};h={'Authorization':'Bearer '+_token('oz_acc1')};P.joinpath('raw').mkdir(exist_ok=True)
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def save(name,obj):P.joinpath(name).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
def call(method,path,body=None,params=None,seller=False):
 if method=='PUT':
  assert args.execute and not seller
  item=next((t for t in TARGETS if path==f"/api/client/campaign/{t['cid']}/products"),None);assert item
  assert body=={'bids':[{'sku':item['sku'],'bid':str(int(D(item['target'])*1000000))}]}
 else:
  assert (not seller and ((method=='GET' and (path=='/api/client/campaign' or any(path==f"/api/client/campaign/{t['cid']}/v2/products" for t in TARGETS) or path=='/api/client/campaign/12704286/v2/products')) or (method=='POST' and path=='/api/client/min/sku'))) or (seller and method=='POST' and path==PRODUCT_INFO_URL)
 event={'at_utc':now(),'method':method,'path':'/v3/product/info/list' if seller else path,'request':body,'params':params,'phase':'WRITE_INTENT' if method=='PUT' else 'READ_INTENT'}
 with (P/'operations.jsonl').open('a') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')
 r=requests.request(method,path if seller else PERF+path,headers=_headers('oz_acc1') if seller else h,json=body,params=params,timeout=120)
 obj={**event,'http':r.status_code,'response':r.json() if r.content else {},'response_at_utc':now()};save('raw/'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S_%f')+'.json',obj);r.raise_for_status();return obj
read=lambda f:json.loads(Path(f).read_text())
protected={r['sku'] for r in csv.DictReader((L/'uploaded_sku_manifest.csv').open(encoding='utf-8-sig'))}|{r['sku'] for r in csv.DictReader((O.parent/'ozon_recovery_20261103/revised_sku_summary_20.csv').open(encoding='utf-8-sig'))};assert not ({t['sku'] for t in TARGETS}&protected)
meta=call('GET','/api/client/campaign')['response']['list'];by={str(c['id']):c for c in meta};save('campaigns_before.json',{'captured_at_utc':now(),'campaigns':meta})
for t in TARGETS:assert by[t['cid']]['state']=='CAMPAIGN_STATE_RUNNING' and by[t['cid']]['productAutopilotStrategy']=='TARGET_BIDS' and not by[t['cid']]['isAutocreated']
skus=[t['sku'] for t in TARGETS]+['5251202928'];cards=call('POST',PRODUCT_INFO_URL,{'sku':skus},seller=True)['response']['items'];model={};readiness=[]
for x in cards:
 aliases=[str(x.get('sku'))]+[str(s.get('sku')) for s in x.get('sources',[])]
 for s in aliases:
  if s in skus:model[s]={'article':x['offer_id'],'model_id':str((x.get('model_info') or {}).get('model_id')),'stock':x['stocks']['has_stock']}
 if x['offer_id'] in ['4838','2306']:assert x['stocks']['has_stock'] and not x.get('is_archived') and not x.get('is_autoarchived') and not x.get('errors')
assert model['1261464375']['article']=='4838' and model['863386567']['article']=='2306';assert model['863386567']['model_id']==model['5251202928']['model_id']=='220510583';save('fresh_exact_model_overlap.json',model)
floors={str(r['sku']):D(str(r['bid'])) for r in call('POST','/api/client/min/sku',{'sku':[t['sku'] for t in TARGETS],'paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU'})['response']['minBids']}
before={}
for t in TARGETS:
 z=call('GET',f"/api/client/campaign/{t['cid']}/v2/products",params={'page':t['page'],'pageSize':20})['response']['products'];before[t['cid']]=z;item=next(r for r in z if str(r['sku'])==t['sku']);value=D(str(item['bid']))/1000000;assert value in [D(t['before']),D(t['target'])],('Unexpected current bid',t['article'],str(value));assert floors[t['sku']]<=D(t['target'])<D(t['before']);t['live_before_rub']=str(value)
cheap=call('GET','/api/client/campaign/12704286/v2/products',params={'page':202,'pageSize':20})['response']['products'];cheapbid=next(x['bid'] for x in cheap if str(x['sku'])=='863386567');assert D(cheapbid)==D(14000000);save('manual_preflight.json',{'targets':TARGETS,'minimum_rub':{k:str(v) for k,v in floors.items()},'unchanged_cheap_2306_micro_rub':cheapbid,'execute':args.execute})
results=[]
for t in TARGETS:
 changed=False
 if args.execute and D(t['live_before_rub'])!=D(t['target']):
  call('PUT',f"/api/client/campaign/{t['cid']}/products",{'bids':[{'sku':t['sku'],'bid':str(int(D(t['target'])*1000000))}]});changed=True
 if args.execute:
  after=call('GET',f"/api/client/campaign/{t['cid']}/v2/products",params={'page':t['page'],'pageSize':20})['response']['products'];item=next(r for r in after if str(r['sku'])==t['sku']);assert D(str(item['bid']))==D(t['target'])*1000000
  prev={str(x['sku']):str(x['bid']) for x in before[t['cid']]};cur={str(x['sku']):str(x['bid']) for x in after};assert set(prev)==set(cur);assert all(prev[k]==cur[k] for k in prev if k!=t['sku'])
  at=now();res={**t,'changed_now':changed,'verified_at_utc':at,'verified_bid_rub':t['target'],'other_page_bids_unchanged':True};results.append(res)
  event={'kind':'LEGACY_MANUAL_BID_REDUCTION','at_utc':at,'article':t['article'],'sku':t['sku'],'campaign_id':t['cid'],'before_bid_rub':t['before'],'after_bid_rub':t['target'],'model_id':model[t['sku']]['model_id'],'overlaps_wave1':t['article']=='2306','wave1_sku':'5251202928' if t['article']=='2306' else None,'wave1_article':'2294X2' if t['article']=='2306' else None,'source_report':'ozon_bid_candidates_20261108/apply_20261108'}
  events=L/'external_interventions.jsonl';existing=[json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
  if not any(e.get('kind')==event['kind'] and e.get('sku')==t['sku'] and e.get('after_bid_rub')==t['target'] for e in existing):
   with events.open('a') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')
  save('manual_result.json',{'state':'PARTIAL' if len(results)<len(TARGETS) else 'BOTH_MANUAL_BIDS_VERIFIED','checked_at_utc':now(),'results':results,'autocreated_campaign_write_calls':0,'created_campaigns':0,'deleted_products':0,'fixed20_changed':False,'wave1_sku_bids_changed':False})
if args.execute:
 cheap_after=call('GET','/api/client/campaign/12704286/v2/products',params={'page':202,'pageSize':20})['response']['products'];assert next(x['bid'] for x in cheap_after if str(x['sku'])=='863386567')==cheapbid
 meta_after=call('GET','/api/client/campaign')['response']['list'];aa={str(c['id']):c for c in meta_after}
 for cid in AUTO:
  assert all(by[cid].get(k)==aa[cid].get(k) for k in ['productAutopilotStrategy','weeklyBudget','expenseStrategy','isAutocreated','title'])
 save('campaigns_after.json',{'captured_at_utc':now(),'campaigns':meta_after})
 print('VERIFIED: 4838 106.29 ->95.66; 2306 high50.17 ->45.15; cheap14 unchanged. Bonus campaigns untouched.')
else:print('DRY_RUN_READY: two exact legacy bid reductions; bonus-campaign write endpoints forbidden.')

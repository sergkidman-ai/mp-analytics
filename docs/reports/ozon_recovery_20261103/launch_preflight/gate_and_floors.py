# поток: mkt
"""Family-level eligibility and numeric individual CPC floors; no marketplace writes."""
from collect_checks import *
import collections,math
D=decimal.Decimal
with (ROOT/'revised_prepared_composition_2953.csv').open() as f:prepared=list(csv.DictReader(f))
byid={};alias={};sources={}
for p in sorted(R.glob('*_info_*.json')):
 z=json.loads(p.read_text())
 if z['http']!=200:continue
 for it in z['body'].get('items',[]):
  pid=str(it['id']);byid[pid]=it;sources[pid]=str(p.relative_to(O))
  for sku in [it.get('sku')]+[x.get('sku') for x in it.get('sources',[])]:
   if sku:alias[str(sku)]=pid
universe=json.loads((O/'bounded_universe.json').read_text());active=set(universe['active_cpc_skus']);active_unknown=active-set(alias)
coverage=json.loads((O/'active_campaign_coverage.json').read_text());active_complete=all(c['http']==200 for c in coverage) and not active_unknown
families=collections.defaultdict(set)
for pid,it in byid.items():
 mid=str(it.get('model_info',{}).get('model_id') or '')
 if mid:families[mid].add(pid)
activefamilies={str(byid[alias[s]].get('model_info',{}).get('model_id') or '') for s in active if s in alias}
def local_errors(it):
 ee=[]
 if it.get('is_archived') or it.get('is_autoarchived'):ee.append('ARCHIVED_PRODUCT')
 try:price=D(str(it.get('price')))
 except Exception:price=None
 if not price or not price.is_finite() or price<=0:ee.append('NO_POSITIVE_PRICE')
 if it.get('currency_code')!='RUB':ee.append('PRICE_CURRENCY_NOT_RUB')
 if not it.get('stocks',{}).get('has_stock'):ee.append('NO_STOCK')
 if not it.get('visibility_details',{}).get('has_price'):ee.append('PRICE_NOT_VISIBLE')
 if not any(a.get('availability')=='AVAILABLE' for a in it.get('availabilities',[])):ee.append('NOT_AVAILABLE')
 if any(str(e.get('level','')).upper()=='ERROR' for e in it.get('errors',[])):ee.append('CARD_ERROR')
 return ee
familyreasons={};familyaudit=[]
relevantmodels={str(byid[alias[r['sku']]]['model_info']['model_id']) for r in prepared if r['sku'] in alias}
for mid in relevantmodels:
 pp=families[mid];counts={int(byid[pid].get('model_info',{}).get('count',0)) for pid in pp};reasons=[]
 if not active_complete:reasons.append('ACTIVE_FAMILY_COVERAGE_INCOMPLETE')
 if mid in activefamilies:reasons.append('FAMILY_ALREADY_IN_RUNNING_CPC')
 if len(counts)!=1 or not next(iter(counts),0) or len(pp)>max(counts):reasons.append('FAMILY_COUNT_INCONSISTENT')
 elif len(pp)<max(counts):reasons.append('FAMILY_COMPOSITION_INCOMPLETE')
 bad={pid:local_errors(byid[pid]) for pid in pp if local_errors(byid[pid])}
 if bad:reasons.append('FAMILY_MEMBER_FAILED_CHECK')
 familyreasons[mid]=reasons
 familyaudit.append({'model_id':mid,'known_products':len(pp),'reported_counts':sorted(counts),'known_product_ids':sorted(pp),'active_family':mid in activefamilies,'failed_member_details':bad,'reasons':reasons,'source':'fresh bounded Seller metadata; current model family, no historical membership claim'})
save('family_audit.json',familyaudit)
gated=[];passed=[]
for r in prepared:
 s=r['sku'];pid=alias.get(s);it=byid.get(pid,{});mid=str(it.get('model_info',{}).get('model_id') or '');err=[]
 if not pid:err.append('PRODUCT_METADATA_MISSING')
 if not mid:err.append('FAMILY_ID_MISSING')
 err+=local_errors(it) if pid else []
 err+=familyreasons.get(mid,['FAMILY_NOT_CHECKED'])
 if s in active:err.append('SKU_ALREADY_IN_RUNNING_CPC')
 row={'sku':s,'stream':r['stream'],'product_id':pid,'offer_id':it.get('offer_id'),'name':it.get('name'),'model_id':mid,'price_rub':it.get('price'),'stock_has_stock':it.get('stocks',{}).get('has_stock'),'availability':it.get('availabilities'),'reasons':sorted(set(err)),'metadata_source':sources.get(pid),'passed_nonfloor_checks':not err}
 gated.append(row)
 if not err:passed.append(row)
save('eligibility_before_floors.json',gated)
print('Nonfloor eligible',len(passed),'by stream',dict(collections.Counter(r['stream'] for r in passed)),flush=True)
print('Family exclusion reasons',dict(collections.Counter(reason for r in gated for reason in r['reasons'])),flush=True)
# Read individual responses for every eligible SKU. Batch reads still return a distinct minBids record per SKU.
ids=[r['sku'] for r in passed]
for i in range(0,len(ids),100):
 name=f'floor_batch_{i//100:03}.json';p=R/name
 if p.exists():continue
 body={'sku':ids[i:i+100],'paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU'}
 for attempt in range(5):
  r=requests.post(PERF+'/api/client/min/sku',headers=H,json=body,timeout=90)
  if r.status_code!=429:break
  time.sleep(min(15,3*(attempt+1)))
 save('raw/'+name,{'request':body,'http':r.status_code,'retrieved_at_utc':now(),'response':r.json(),'endpoint':'POST /api/client/min/sku','paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU'})
 print('Individual floor batch',i//100,'HTTP',r.status_code,'returned records',len(r.json().get('minBids',[])),flush=True);time.sleep(.3)
values={};badfloor={}
for p in R.glob('floor_batch_*.json'):
 z=json.loads(p.read_text());asked=z['request']['sku'];found=collections.defaultdict(list)
 if z['http']==200:
  for x in z['response'].get('minBids',[]):found[str(x.get('sku'))].append(x)
 for s in asked:
  rec=found.get(s,[]);reason=None
  if z['http']!=200:reason='FLOOR_HTTP_'+str(z['http'])
  elif len(rec)!=1:reason='FLOOR_MISSING_OR_DUPLICATE'
  elif isinstance(rec[0].get('bid'),bool) or not isinstance(rec[0].get('bid'),(int,float)):reason='FLOOR_NOT_NUMERIC'
  else:
   bid=D(str(rec[0]['bid']))
   if not bid.is_finite() or bid<=0:reason='FLOOR_NOT_POSITIVE_FINITE'
   elif bid*D(1000000)!=(bid*D(1000000)).to_integral_value():reason='FLOOR_MICRO_CONVERSION_NOT_EXACT'
  if reason:badfloor[s]=reason
  else:values[s]={'sku':s,'bid_api':rec[0]['bid'],'bid_rub':str(bid),'bid_micro_rub':str(int(bid*D(1000000))),'api_unit':'RUB per click','product_write_unit':'one millionth RUB','source':str(p.relative_to(O)),'checked_at_utc':z['retrieved_at_utc'],'paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU','individual_response_record':rec[0]}
# Apply floor failures to all recovered members of the same family.
failedmodels={r['model_id'] for r in passed if r['sku'] not in values}
for row in gated:
 s=row['sku']
 if row['passed_nonfloor_checks']:
  if s in badfloor:row['reasons'].append(badfloor[s])
  if row['model_id'] in failedmodels:row['reasons'].append('FAMILY_MEMBER_FLOOR_FAILED')
 row['passed_all_checks']=not row['reasons']
 row['floor']=values.get(s)
save('eligibility_final.json',gated);save('individual_floors.json',list(values.values()));save('floor_errors.json',badfloor)
print('FINAL eligible',sum(r['passed_all_checks'] for r in gated),'floor errors',len(badfloor),flush=True)

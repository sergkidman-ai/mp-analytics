# поток: mkt
"""Offline analysis of authentic exports; missing rows stay missing, never zero-filled."""
import csv,json,pathlib,datetime as dt,collections,decimal,hashlib
from zoneinfo import ZoneInfo
OUT=pathlib.Path(__file__).parent;RAW=OUT/'raw';D=decimal.Decimal;MSK=ZoneInfo('Europe/Moscow')
SKUS='1510947813 863386191 1719404303 1277712843 930627436 2909045114 930623456 1658970308 866442961 1999321262 3557209622 906874407 4504239311 4518308193 652626682 4463532700 4480826404 4518304506 4489633315 4518302691'.split();TARGET=set(SKUS)
EXCLUDED={'49173478','49173499','49173502'}
def load(p):return json.loads(p.read_text())
def save(n,v):(OUT/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def write(n,rows,fields=None):
 if fields is None:fields=list(rows[0]) if rows else []
 with (OUT/n).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def num(x):
 if x is None:return None
 x=str(x).strip().replace('\u00a0','').replace(' ','').replace(',','.')
 if x in ['', '-', '—']:return None
 return D(x)
def date(x):
 for fmt in ['%Y-%m-%d','%d.%m.%Y']:
  try:return dt.datetime.strptime(x,fmt).date().isoformat()
  except ValueError:pass
 raise ValueError('Unrecognized date format')
def msk(x):return dt.datetime.fromisoformat(x.replace('Z','+00:00')).astimezone(MSK).isoformat() if x else ''
def add(a,key,v):
 if v is not None:a[key]=a.get(key,D(0))+v
metrics=['views','clicks','spend_rub','ad_orders_api','ad_revenue_rub']
cs=load(RAW/'campaigns.json')['list'];meta={str(c['id']):c for c in cs}
inv=[]
for c in cs:
 cid=str(c['id']);inv.append({'campaign_id':cid,'title':c.get('title'),'state':c.get('state'),'type':c.get('advObjectType'),'payment_type':c.get('PaymentType'),'created_at_utc':c.get('createdAt'),'created_at_msk':msk(c.get('createdAt')),'from_date':c.get('fromDate'),'to_date':c.get('toDate'),'updated_at_utc':c.get('updatedAt'),'is_autocreated':c.get('isAutocreated'),'source':'raw/campaigns.json'})
write('campaign_inventory.csv',inv)
products={};current=[];pairset=set()
for p in sorted(RAW.glob('products_*.json')):
 v=load(p);cid=p.stem.removeprefix('products_');products[cid]=v
 for x in v['products']:
  sku=str(x.get('sku'))
  if sku not in TARGET:continue
  pairset.add((sku,cid))
  current.append({'sku':sku,'campaign_id':cid,'campaign_title':meta[cid]['title'],'campaign_state':meta[cid]['state'],'title':x.get('title'),'bid_micro_rub':x.get('bid'),'bid_rub':num(x.get('bid'))/D(1000000) if num(x.get('bid')) is not None else None,'retrieved_at_utc':v['retrieved_at_utc'],'source':str(p.relative_to(OUT))})
assert len(current)==len(pairset)
write('current_bids.csv',current)
daily=[];dk=set()
for p in sorted(RAW.glob('daily_*.json')):
 z=load(p)
 if z['http']!=200:continue
 for x in z['body'].get('rows') or []:
  cid=str(x['id']);day=date(x['date']);key=(cid,day);assert key not in dk;dk.add(key)
  c=meta[cid];payorder=c.get('advObjectType') in ['SEARCH_PROMO','ALL_SKU_PROMO']
  v={'campaign_id':cid,'title':c['title'],'state_now':c['state'],'type':c.get('advObjectType'),'date_msk':day,'views':num(x.get('views')),'clicks':num(x.get('clicks')),'spend_rub':num(x.get('moneySpent')),'ad_orders_api':None if payorder else num(x.get('orders')),'ad_revenue_rub':None if payorder else num(x.get('ordersMoney')),'orders_status':'нет данных: ограничение CPO в действующем клиенте' if payorder else 'reported','partial_current_day':day=='2026-11-03','source':str(p.relative_to(OUT))}
  v['cpc_rub']=v['spend_rub']/v['clicks'] if v['clicks'] else None;daily.append(v)
write('campaign_daily.csv',daily)
candidates=[];ccomps=[];candidate_daily=[]
for c in cs:
 cid=str(c['id']);created=msk(c.get('createdAt'));created_day=created[:10]
 if cid in EXCLUDED or c.get('PaymentType')!='CPC' or not ('2026-11-01'<=created_day<='2026-11-03'):continue
 obs=[x for x in daily if x['campaign_id']==cid and any(x[k] and x[k]>0 for k in ['views','clicks','spend_rub'])]
 p=products.get(cid,{});ps=p.get('products',[])
 candidates.append({'campaign_id':cid,'title':c['title'],'state':c['state'],'created_at_utc':c.get('createdAt'),'created_at_msk':created,'from_date':c.get('fromDate'),'to_date':c.get('toDate'),'is_autocreated':c.get('isAutocreated'),'current_composition_count':len(ps) if p.get('http')==200 else None,'wave_membership':'не подтверждена','membership_reason':'финальный манифест недоступен; дата создания и название не доказывают принадлежность','operation_confirmed':bool(obs),'operation_evidence':';'.join(sorted(set(x['date_msk'] for x in obs))),'activation_timestamp':None,'november_2_partial_day':created_day=='2026-11-02','metadata_source':'raw/campaigns.json','composition_source':f'raw/products_{cid}.json'})
 for x in ps:ccomps.append({'campaign_id':cid,'sku':x.get('sku'),'title':x.get('title'),'bid_micro_rub':x.get('bid'),'source':f'raw/products_{cid}.json','status':'текущий состав; состав на запуск не подтверждён'})
 for day in ['2026-11-01','2026-11-02','2026-11-03']:
  got=next((x for x in daily if x['campaign_id']==cid and x['date_msk']==day),None)
  rec={'campaign_id':cid,'date_msk':day,'status':'reported' if got else 'нет данных','wave_membership':'не подтверждена','partial_day':day=='2026-11-03' or (day=='2026-11-02' and created_day=='2026-11-02')}
  for k in metrics+['cpc_rub']:rec[k]=got[k] if got else None
  rec['source']=got['source'] if got else None;candidate_daily.append(rec)
write('wave_candidates.csv',candidates)
write('wave_candidates_composition.csv',ccomps)
write('wave_candidates_daily.csv',candidate_daily)
write('wave_excluded_preparation.csv',[{'campaign_id':cid,'title':meta[cid]['title'],'state':meta[cid]['state'],'current_composition_count':len(products[cid]['products']),'reason':'исключён пользователем: подготовительный ID','source':f'raw/products_{cid}.json'} for cid in sorted(EXCLUDED)])
save('wave_assessment.json',{'candidate_ids':[r['campaign_id'] for r in candidates],'confirmed_wave_campaign_ids':[],'manifest_available':False,'wave_membership_confirmed':False,'actual_launched_wave_sku_count':None,'wave_spend_total_rub':None,'reference_rub':12500,'deviation_rub':None,'reason':'Финальный состав волны не подтверждён; итог по волне и отклонение не рассчитываются. Подготовленные 2953 SKU не считаются запущенными.','activation_history':'не получена: в существующих клиентах нет метода истории активации; работа кандидата подтверждена статистикой','candidate_metadata_window_msk':['2026-11-01','2026-11-03'],'partial_current_day':'2026-11-03'})
# CSV supplies explicit integer zeros omitted in JSON. Never turn an absent row into zero.
colmap={'views':'Показы','clicks':'Клики','spend_rub':'Расход, ₽, с НДС','ad_orders_api':'Продано товаров','ad_revenue_rub':'Продажи в продвижении, ₽'}
allcsv={};duplicates=[]
for folder in sorted(RAW.glob('csv_2026-*')):
 if not folder.is_dir():continue
 for p in sorted(folder.glob('*.csv')):
  cid=p.name.split('_')[0]
  with p.open(encoding='utf-8-sig',newline='') as f:
   next(f);reader=csv.DictReader(f,delimiter=';')
   for row in reader:
    sku=str(row.get('sku',''))
    if sku not in TARGET:continue
    day=date(row['День']);key=(sku,cid,day)
    rec={'sku':sku,'campaign_id':cid,'date_msk':day,**{k:num(row.get(col)) for k,col in colmap.items()},'source':str(p.relative_to(OUT))}
    if key in allcsv:duplicates.append(key)
    allcsv[key]=rec
assert not duplicates
# Check all 31 CPC campaign JSON results for historical target pairs outside current mapping.
jsonrows={};jsonfilecoverage=[];historical_extra=[]
for p in sorted(RAW.glob('sku_report*_result.json')):
 z=load(p);b=z['body'];valid=z['http']==200 and 'text' not in b
 count=0
 if valid:
  for cid,block in b.items():
   if not isinstance(block,dict):continue
   rr=block.get('report',{}).get('rows') or [];count+=len(rr)
   for row in rr:
    sku=str(row.get('sku'))
    if sku not in TARGET:continue
    key=(sku,str(cid),date(row['date']));assert key not in jsonrows
    jsonrows[key]=row
    if key not in allcsv:
     historical_extra.append({'sku':sku,'campaign_id':str(cid),'date_msk':key[2],**{k:num(row.get(col)) for k,col in {'views':'views','clicks':'clicks','spend_rub':'moneySpent','ad_orders_api':'orders','ad_revenue_rub':'ordersMoney'}.items()},'source':str(p.relative_to(OUT))})
 jsonfilecoverage.append({'source':str(p.relative_to(OUT)),'http':z['http'],'valid_json':valid,'campaign_blocks':len(b) if valid else None,'rows_all_skus':count,'empty_response':valid and count==0})
# CSV/JSON cross-check: protobuf-style omitted integer zeros are compared for validation only.
matched=0;mismatches=[]
for key,row in jsonrows.items():
 if key not in allcsv:continue
 x=allcsv[key];matched+=1
 for k,col in {'views':'views','clicks':'clicks','spend_rub':'moneySpent','ad_orders_api':'orders','ad_revenue_rub':'ordersMoney'}.items():
  a=num(row.get(col,0));b=x[k]
  if a!=b:mismatches.append({'key':key,'field':k,'json':a,'csv':b})
assert not mismatches
records=list(allcsv.values())+historical_extra
records=[x for x in records if '2026-10-02'<=x['date_msk']<='2026-11-02']
records.sort(key=lambda x:(SKUS.index(x['sku']),x['campaign_id'],x['date_msk']))
for x in records:x['cpc_rub']=x['spend_rub']/x['clicks'] if x['clicks'] else None
write('sku_campaign_daily.csv',records)
write('sku_report_coverage.csv',jsonfilecoverage)
index={(r['sku'],r['campaign_id'],r['date_msk']):r for r in records}
allpairs=pairset|{(x['sku'],x['campaign_id']) for x in records}
days=[(dt.date(2026,10,2)+dt.timedelta(days=i)).isoformat() for i in range(32)]
base=[d for d in days if d<='2026-10-29'];post=['2026-10-31','2026-11-01','2026-11-02']
matched_dates={d:[b for b in base if dt.date.fromisoformat(b).weekday()==dt.date.fromisoformat(d).weekday()] for d in post};assert all(len(v)==4 for v in matched_dates.values())
needed=set(post)|set(sum(matched_dates.values(),[]));assert len(needed)==15
paircover=[];eligible=set()
for sku,cid in sorted(allpairs):
 observed=[d for d in days if (sku,cid,d) in index];missing=[d for d in sorted(needed) if (sku,cid,d) not in index]
 complete=not missing and all(all(index[(sku,cid,d)][m] is not None for m in metrics) for d in needed)
 if complete:eligible.add((sku,cid))
 paircover.append({'sku':sku,'campaign_id':cid,'found_in_current_composition':(sku,cid) in pairset,'observed_days':len(observed),'requested_days':32,'missing_days':';'.join(d for d in days if d not in observed),'comparison_complete':complete,'missing_comparison_days':';'.join(missing),'status':'нет данных' if not observed else 'reported'})
write('sku_pair_coverage.csv',paircover)
cover=[];nov2=[]
for sku in SKUS:
 pairs=[p for p in allpairs if p[0]==sku]
 for day in days:
  rr=[index[(sku,cid,day)] for _,cid in pairs if (sku,cid,day) in index]
  v={'sku':sku,'date_msk':day,'status':'reported' if rr else 'нет данных','observed_campaigns':';'.join(sorted(x['campaign_id'] for x in rr)),'observed_pairs':len(rr),'found_pairs':len(pairs),'missing_pairs':';'.join(cid for _,cid in sorted(pairs) if (sku,cid,day) not in index)}
  for m in metrics:v[m]=sum((x[m] for x in rr),D(0)) if rr and all(x[m] is not None for x in rr) else None
  v['cpc_rub']=v['spend_rub']/v['clicks'] if v['clicks'] else None
  cover.append(v)
  if day=='2026-11-02':nov2.append(v.copy())
write('sku_day_coverage.csv',cover);write('sku_20261102.csv',nov2)
# Compare a fixed cohort of pairs with all 12 reference days and all 3 post days explicitly returned.
comparisons=[];paircomparisons=[]
def compare(sku,pairs):
 out={'sku':sku,'eligible_pairs':len(pairs),'campaign_ids':';'.join(sorted(cid for _,cid in pairs)),'status':'comparable reported subset' if pairs else 'нет данных: недостаточное покрытие'}
 for m in metrics:
  out['post_'+m]=sum((index[(sku,cid,d)][m] for _,cid in pairs for d in post),D(0)) if pairs else None
  out['expected_'+m]=sum((sum((index[(sku,cid,b)][m] for b in matched_dates[d]),D(0))/D(4) for _,cid in pairs for d in post),D(0)) if pairs else None
  out['delta_'+m]=out['post_'+m]-out['expected_'+m] if pairs else None
 out['spend_delta_percent']=D(100)*out['delta_spend_rub']/out['expected_spend_rub'] if out['expected_spend_rub'] else None
 return out
for sku in SKUS:
 pp={p for p in eligible if p[0]==sku};comparisons.append(compare(sku,pp))
 for pair in sorted(pp):paircomparisons.append(compare(sku,{pair}))
write('comparison_by_sku.csv',comparisons);write('comparison_by_pair.csv',paircomparisons)
agg={'status':'только сопоставимая часть; не полный итог кабинета','eligible_pairs':len(eligible),'eligible_skus':len(set(s for s,c in eligible)),'requested_skus':20,'all_found_pairs':len(allpairs),'excluded_pairs':len(allpairs-eligible)}
for key in ['post_spend_rub','expected_spend_rub','delta_spend_rub','post_ad_orders_api','expected_ad_orders_api','delta_ad_orders_api']:
 agg[key]=sum((x[key] for x in comparisons if x[key] is not None),D(0)) if eligible else None
agg['spend_delta_percent']=D(100)*agg['delta_spend_rub']/agg['expected_spend_rub'] if agg['expected_spend_rub'] else None
save('comparison_summary.json',agg)
byday=[]
for d in post:
 v={'date_msk':d,'reference_dates':matched_dates[d],'eligible_pairs':len(eligible)}
 for m in ['spend_rub','ad_orders_api']:
  v['post_'+m]=sum((index[(s,c,d)][m] for s,c in eligible),D(0))
  v['expected_'+m]=sum((sum((index[(s,c,b)][m] for b in matched_dates[d]),D(0))/D(4) for s,c in eligible),D(0))
  v['delta_'+m]=v['post_'+m]-v['expected_'+m]
 v['spend_delta_percent']=D(100)*v['delta_spend_rub']/v['expected_spend_rub'] if v['expected_spend_rub'] else None
 byday.append(v)
write('comparison_by_day.csv',byday)
save('comparison_design.json',{'timezone':'Europe/Moscow','baseline':['2026-10-02','2026-10-29'],'mixed_day_excluded':'2026-10-30','post_period':post,'same_weekdays':matched_dates,'method':'Fixed SKU × campaign pairs with explicit metrics for all 15 comparison dates. Expected post spend is sum of four-date weekday means. Delta=post−expected. Percentage=100*delta/expected, only nonzero denominator. Missing rows never zero-filled.','initial_bid_manifest_available':False,'rollback_confirmed':False,'causality_claimed':False})
reconciliation=[]
for cid in sorted(set(c for s,c in allpairs)):
 rr=[x for x in records if x['campaign_id']==cid];reconciliation.append({'campaign_id':cid,'target_sku_rows':len(rr),'target_skus':len(set(x['sku'] for x in rr)),'days':len(set(x['date_msk'] for x in rr))})
coverage={'requested_skus':20,'observed_skus':len(set(x['sku'] for x in records)),'requested_days':32,'observed_days_any_sku':len(set(x['date_msk'] for x in records)),'requested_sku_days':640,'observed_sku_days':sum(v['status']=='reported' for v in cover),'missing_sku_days':sum(v['status']!='reported' for v in cover),'current_bid_pairs':len(current),'all_observed_or_current_pairs':len(allpairs),'sku_campaign_day_rows':len(records),'nov2_skus_observed':sum(v['status']=='reported' for v in nov2),'csv_json_rows_crosschecked':matched,'csv_json_metric_mismatches':len(mismatches),'extra_historical_target_rows_without_csv':len(historical_extra),'sku_statistics_campaigns':reconciliation,'product_endpoints':{'success':sum(v['http']==200 for v in products.values()),'failed':sum(v['http']!=200 for v in products.values())},'empty_json_campaign_windows':[]}
for p in sorted(RAW.glob('sku_report*_result.json')):
 for cid,block in load(p)['body'].items():
  if isinstance(block,dict) and not (block.get('report',{}).get('rows') or []):coverage['empty_json_campaign_windows'].append({'campaign_id':cid,'source':str(p.relative_to(OUT)),'status':'нет данных'})
save('coverage_summary.json',coverage)
# Top deviations, both directions, kept to comparable subset only.
ranked=sorted([x for x in comparisons if x['delta_spend_rub'] is not None],key=lambda x:abs(x['delta_spend_rub']),reverse=True)
write('largest_deviations.csv',ranked)
print('Coverage:',coverage['observed_skus'],'SKU,',coverage['observed_sku_days'],'/640 SKU-days,',len(records),'SKU-campaign-days')
print('Current bid pairs:',len(current),'eligible comparison pairs:',len(eligible),'eligible SKU:',agg['eligible_skus'])
print('Comparable subset spend: post',agg['post_spend_rub'],'expected',agg['expected_spend_rub'],'delta',agg['delta_spend_rub'],'percent',agg['spend_delta_percent'])
print('Comparable subset ad orders: post',agg['post_ad_orders_api'],'expected',agg['expected_ad_orders_api'],'delta',agg['delta_ad_orders_api'])
print('Nov 2 observed metrics:',{m:str(sum((v[m] for v in nov2 if v[m] is not None),D(0))) for m in metrics})
print('Crosscheck CSV/JSON:',matched,'rows,',len(mismatches),'mismatches; extra historical rows',len(historical_extra))
print('Most deviant SKU:',[(x['sku'],str(x['delta_spend_rub']),str(x['delta_ad_orders_api'])) for x in ranked[:5]])

# поток: mkt / ozon
"""Offline acc2 CPC cash/bonus, current stock, all placements and model sales."""
import collections,csv,datetime as dt,json,sys
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price
O=Path(__file__).parent;R=O/'raw';TZ=dt.timezone(dt.timedelta(hours=3));read=lambda p:json.loads(Path(p).read_text());csvread=lambda p:list(csv.DictReader(Path(p).open(encoding='utf-8-sig')));save=lambda n,x:(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def outcsv(name,rows):
 fields=list(dict.fromkeys(k for r in rows for k in r))
 with (O/name).open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
meta={str(c['id']):c for c in read(O/'live_campaigns.json')['response']['list']};fund=collections.defaultdict(lambda:[D(0),D(0),D(0)]);num=lambda x:D(str(x or 0).replace(' ','').replace('\xa0','').replace(',','.'))
for f in R.glob('funding28_*.json'):
 z=read(f);assert z['account']=='oz_acc2' and z['http']==200
 for r in z['response']['rows']:
  assert all(k in r and r[k] is not None for k in ['moneySpent','bonusSpent','prepaymentSpent'])
  for i,k in enumerate(['moneySpent','bonusSpent','prepaymentSpent']):fund[str(r['id'])][i]+=num(r[k])
purepaid={cid for cid,(cash,bonus,prepay) in fund.items() if cash+prepay>0 and bonus==0 and meta[cid].get('advObjectType')=='SKU'}
cat={};alias={};info_observed={}
for f in sorted(R.glob('info_*.json')):
 z=read(f);assert z['account']=='oz_acc2' and z['http']==200
 for x in z['response']['items']:
  src=x.get('sources') or [];main=x.get('sku') or next((s['sku'] for s in src if s.get('source')=='fbs'),None) or next((s['sku'] for s in src if s.get('sku')),None);assert main
  sku=str(main);available=sum(max(D(0),num(s.get('present'))-num(s.get('reserved'))) for s in (x.get('stocks') or {}).get('stocks',[]));c={'sku':sku,'product_id':str(x['id']),'article':x['offer_id'],'name':x['name'],'model_id':str((x.get('model_info') or {}).get('model_id') or ''),'has_stock':(x.get('stocks') or {}).get('has_stock') is True,'available_units':str(available),'available_channel':any(a.get('availability')=='AVAILABLE' for a in x.get('availabilities') or []),'archived':bool(x.get('is_archived') or x.get('is_autoarchived')),'errors':len(x.get('errors') or []),'metadata_observed_at_utc':z['at_utc']};cat[sku]=c
  for a in [main]+[s.get('sku') for s in src]:
   if a:alias[str(a)]=sku
save('current_card_map.json',{'account':'oz_acc2','cards':cat,'canonical_by_alias':alias})
active=collections.defaultdict(list)
for f in R.glob('current_products_*.json'):
 z=read(f);assert z['account']=='oz_acc2' and z['http']==200;cid=str(z['campaign_id'])
 for i,r in enumerate(z['products']):
  raw=str(r['sku']);sku=alias.get(raw,raw)
  if cid in purepaid and meta[cid]['state']=='CAMPAIGN_STATE_RUNNING':active[sku].append({'campaign_id':cid,'raw_sku':raw,'bid_rub':str(num(r['bid'])/1000000),'strategy':meta[cid]['productAutopilotStrategy'],'cached_index':i,'source':str(f),'observed_at_utc':z['captured_at_utc']})
ad=[]
for r in csvread(O/'raw_ad_placements_28d.csv'):
 raw=r['raw_sku'];sku=alias.get(raw,raw);ad.append({**r,'sku':sku,'model_id':cat.get(sku,{}).get('model_id','')})
sales=[];seen=set();cancelled=0
for scheme in ['fbs','fbo']:
 z=read(R/f'postings_{scheme}.json');assert z['account']=='oz_acc2' and z['complete']
 for p in z['postings']:
  key=(scheme,p['posting_number']);assert key not in seen;seen.add(key)
  if p['status'].lower().startswith('cancel'):cancelled+=1;continue
  day=dt.datetime.fromisoformat((p.get('in_process_at') or p['created_at']).replace('Z','+00:00')).astimezone(TZ).date().isoformat();assert '2026-10-11'<=day<='2026-11-07'
  for x in p['products']:
   raw=str(x['sku']);sku=alias.get(raw,raw);sales.append({'account':'oz_acc2','sku':sku,'model_id':cat.get(sku,{}).get('model_id',''),'date_msk':day,'order_id':str(p['order_id']),'ordered_value_rub':str(product_price(x)*num(x['quantity']))})
assert all(r['model_id'] for r in sales),'Incomplete sold-product model metadata'
outcsv('private_general_sales_lines.csv',sales);metrics={};model_metrics={}
for label,start in [('28d','2026-10-11'),('8d','2026-10-31')]:
 own=collections.defaultdict(lambda:{'spend':D(0),'sales':D(0),'clicks':D(0),'views':D(0),'adrows':0});mcost=collections.defaultdict(lambda:D(0));val=collections.defaultdict(lambda:D(0));mval=collections.defaultdict(lambda:D(0));orders=collections.defaultdict(set);mo=collections.defaultdict(set)
 for r in ad:
  if r['date_msk']<start or r['campaign_id'] not in purepaid:continue
  a=own[r['sku']];a['spend']+=num(r['confirmed_paid_CPC_rub']);a['sales']+=num(r['attributed_own_sales_rub'])+num(r['attributed_model_sales_rub']);a['clicks']+=num(r['clicks']);a['views']+=num(r['views']);a['adrows']+=1
  if r['model_id']:mcost[r['model_id']]+=num(r['confirmed_paid_CPC_rub'])
 for r in sales:
  if r['date_msk']<start:continue
  v=num(r['ordered_value_rub']);s=r['sku'];m=r['model_id'];orders[s].add(r['order_id']);val[s]+=v;mo[m].add(r['order_id']);mval[m]+=v
 for sku in set(own)|set(orders):
  a=own[sku];m=cat.get(sku,{}).get('model_id','');metrics.setdefault(sku,{}).update({label+'_paid_CPC_rub':str(a['spend']) if a['adrows'] else '',label+'_attributed_sales_with_model_rub':str(a['sales']) if a['adrows'] else '',label+'_clicks':str(a['clicks']) if a['adrows'] else '',label+'_actual_CPC_rub':str(a['spend']/a['clicks']) if a['clicks'] else '',label+'_attribution_DRR_percent':str(a['spend']/a['sales']*100) if a['sales'] else '',label+'_SKU_unique_orders':len(orders[sku]),label+'_SKU_ordered_value_rub':str(val[sku]),label+'_model_unique_orders':len(mo[m]) if m else '',label+'_model_ordered_value_rub':str(mval[m]) if m else '',label+'_model_paid_CPC_rub':str(mcost[m]) if m else '',label+'_model_CPC_to_ordered_percent':str(mcost[m]/mval[m]*100) if m and mval[m] else ''})
 models=set(mcost)|set(mval);model_metrics[label]={m:{'paid_CPC_rub':str(mcost[m]),'unique_orders':len(mo[m]),'ordered_value_rub':str(mval[m])} for m in models}
rank=[]
for sku,placements in active.items():
 c=cat.get(sku);m=metrics.get(sku,{})
 if not c or not c['has_stock'] or num(c['available_units'])<=0 or not c['available_channel'] or c['archived'] or c['errors']:continue
 sp=num(m.get('28d_paid_CPC_rub'))
 if sp<=0:continue
 r={**c,**m,'current_placements':placements};r['preliminary_concern']='NO_GENERAL_SKU_OR_MODEL_SALES' if m.get('28d_model_unique_orders')==0 else 'HIGH_ATTRIBUTION_DRR' if m.get('28d_attribution_DRR_percent') and num(m['28d_attribution_DRR_percent'])>=25 else 'NO_ATTRIBUTED_SALES' if not num(m.get('28d_attributed_sales_with_model_rub')) else 'REVIEW_CONTEXT';rank.append(r)
rank.sort(key=lambda r:num(r['28d_paid_CPC_rub']),reverse=True);save('ranked_instock_paid_skus.json',rank);save('model_metrics.json',model_metrics)
concern=[r for r in rank if r['preliminary_concern']!='REVIEW_CONTEXT'];top=concern[:60]+rank[:12];check=sorted({r['sku'] for r in top});save('minimum_check_skus.json',{'account':'oz_acc2','skus':check})
all_spend=sum(num(r['confirmed_paid_CPC_rub']) for r in ad);zero_model=[r for r in metrics if metrics[r].get('28d_model_unique_orders')==0 and num(metrics[r].get('28d_paid_CPC_rub'))>0];zero_instock=[r for r in rank if r['preliminary_concern']=='NO_GENERAL_SKU_OR_MODEL_SALES'];summary={'account':'oz_acc2','period':['2026-10-11','2026-11-07'],'recent':['2026-10-31','2026-11-07'],'raw_CPC_positive_rows':len(ad),'metadata_cards':len(cat),'metadata_aliases':len(alias),'paid_CPC_100pct_money_from_SKU_reports':str(all_spend),'cash_CPC_all_campaigns':str(sum(v[0]+v[2] for cid,v in fund.items() if meta[cid].get('advObjectType')=='SKU')),'cash_CPO':str(sum(v[0]+v[2] for cid,v in fund.items() if meta[cid].get('PaymentType')=='CPO')),'bonuses':str(sum(v[1] for v in fund.values())),'cash_total':str(sum(v[0]+v[2] for v in fund.values())),'postings':len(seen),'cancelled_postings':cancelled,'account_unique_orders':len({r['order_id'] for r in sales}),'account_ordered_value_rub':str(sum(num(r['ordered_value_rub']) for r in sales)),'unmapped_sold_model_lines':sum(not r['model_id'] for r in sales),'unmapped_paid_CPC_rub':str(sum(num(r['confirmed_paid_CPC_rub']) for r in ad if not r['model_id'])),'instock_active_paid_skus_positive_spend':len(rank),'instock_active_paid_skus_spend50':sum(num(r['28d_paid_CPC_rub'])>=50 for r in rank),'zero_model_sales_instock_skus_positive_spend':len(zero_instock),'zero_model_sales_instock_skus_spend50':sum(num(r['28d_paid_CPC_rub'])>=50 for r in zero_instock),'zero_model_sales_instock_spend50':str(sum(num(r['28d_paid_CPC_rub']) for r in zero_instock if num(r['28d_paid_CPC_rub'])>=50)),'zero_model_sales_instock_spend':str(sum(num(r['28d_paid_CPC_rub']) for r in zero_instock)),'cash_skus_no_model_orders_all_stock_states':len(zero_model),'cash_CPC_no_model_orders_all_stock_states':str(sum(num(metrics[s]['28d_paid_CPC_rub']) for s in zero_model)),'minimum_checks_requested':len(check),'marketplace_mutations':0,'production_DB_writes':0};save('analysis_summary_prescreen.json',summary)
print(json.dumps(summary,ensure_ascii=False))
for r in rank[:16]:print(json.dumps({k:r.get(k) for k in ['sku','article','28d_paid_CPC_rub','28d_actual_CPC_rub','28d_attribution_DRR_percent','28d_SKU_unique_orders','28d_model_unique_orders','28d_model_ordered_value_rub','preliminary_concern']},ensure_ascii=False))

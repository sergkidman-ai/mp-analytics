# поток: mkt / ozon
"""Offline CPC evidence, all returned placements; no network or production DB writes."""
import collections,csv,datetime as dt,json,sys
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price
P=Path('/opt/mp-analytics/docs/reports');O=Path(__file__).parent;Y=P/'ozon_year_review_20261105';L=P/'ozon_launch_20261106';B=L/'baseline';C=P/'ozon_full_inventory_20261105'
START,END='2026-10-11','2026-11-07';RECENT='2026-10-31';TZ=dt.timezone(dt.timedelta(hours=3))
read=lambda f:json.loads(Path(f).read_text())
rows=lambda f:list(csv.DictReader(Path(f).open(encoding='utf-8-sig')))
save=lambda n,x:(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def csvout(name,data):
 with (O/name).open('w',encoding='utf-8-sig',newline='') as stream:
  w=csv.DictWriter(stream,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
def number(x):
 if x in [None,'','—','-']:return D(0)
 return D(str(x).replace(' ','').replace('\xa0','').replace(',','.'))
def day(x):
 if '.' in x:return dt.datetime.strptime(x,'%d.%m.%Y').date().isoformat()
 return x[:10]
maps=read(Path(read(L/'risk_policy.json')['family_maps_path']));aliases=maps['canonical_by_alias'];models=maps['model_by_sku_alias'];cat={r['sku']:r for r in rows(C/'all_current_catalog.csv')};index={};source_stats=collections.Counter()
def add(cid,raw,date,views,clicks,spend,own,model,orders,source):
 if not str(raw).isdigit() or not START<=date<=END:return
 values=[number(x) for x in [views,clicks,spend,own,model,orders]]
 if not any(values):return
 key=(str(cid),str(raw),date);assert key not in index,('Duplicate raw campaign/SKU/day',key)
 sku=aliases.get(str(raw),str(raw));index[key]={'sku':sku,'raw_sku':str(raw),'campaign_id':str(cid),'date_msk':date,'views':str(values[0]),'clicks':str(values[1]),'spend_rub':str(values[2]),'attributed_own_sales_rub':str(values[3]),'attributed_model_sales_rub':str(values[4]),'own_ad_units':str(values[5]),'model_id':models.get(str(raw),models.get(sku,'')),'source':str(source)};source_stats[str(source)]+=1
# Reuse the original October reports, which retain model sales omitted from the annual flat summary.
for f in read(Y/'sku_report_jobs.json')['reused_sku_report_files']:
 z=read(f)['body']
 for cid,block in z.items():
  for r in (block.get('report') or {}).get('rows',[]):
   date=day(r.get('date',''))
   if START<=date<='2026-10-31':add(cid,r.get('sku'),date,r.get('views'),r.get('clicks'),r.get('moneySpent'),r.get('ordersMoney'),r.get('modelsMoney'),r.get('orders'),f)
 print('Parsed reused October source',Path(f).name,flush=True)
def parse_csv(f,allowed):
 cid=f.name.split('_')[0];assert cid.isdigit(),f
 with f.open(encoding='utf-8-sig',newline='') as stream:
  next(stream,None)
  for r in csv.DictReader(stream,delimiter=';'):
   if not str(r.get('sku','')).isdigit():continue
   date=day(r['День'])
   if date in allowed:add(cid,r['sku'],date,r.get('Показы'),r.get('Клики'),r.get('Расход, ₽, с НДС'),r.get('Продажи в продвижении, ₽'),r.get('Продажи в продвижении с заказов модели, ₽'),r.get('Продано товаров'),f)
for folder in (Y/'sku_reports').glob('sku_2026-11-01_2026-11-04_*'):
 assert (folder/'complete.json').exists()
 for f in folder.glob('*.csv'):parse_csv(f,{'2026-11-01','2026-11-02','2026-11-03','2026-11-04'})
def fast(f,allowed):
 z=read(f)
 for r in z.get('response',{}).get('rows',[]):
  date=r['date'][:10]
  if date in allowed:add(r['campaignId'],r['sku'],date,r['views'],r['clicks'],r['expense'],r['sales'],r.get('modelSales'),r['orders'],f)
for f in B.glob('fast_stats_*.json'):fast(f,{'2026-11-05'})
for f in (P/'ozon_recovery_20261103/daily_20261107/raw/monitor20_20261031_20261106').glob('*.csv'):parse_csv(f,{'2026-11-06'})
gap=O/'sku_reports/gap_2026-11-06';assert (gap/'complete.json').exists()
for f in gap.glob('*.csv'):parse_csv(f,{'2026-11-06'})
for f in (O/'raw').glob('*.json'):
 z=read(f)
 if z.get('path')=='/api/client/statistics/products/sku' and z.get('http')==200:fast(f,{'2026-11-07'})
# Include experiment exposure in whole-model costs, while excluding its SKUs from candidates below.
launch=read(L/'first_delivery_latest.json')
for date in ['2026-11-06','2026-11-07']:
 fast(Path(launch['ad_source_by_day'][date]['sku']['path']),{date})
ad=list(index.values());csvout('all_returned_ad_placements_28d.csv',ad)
# Reuse historical postings and replace each posting with its latest observed version.
files=[B/'postings_fbs.json',B/'postings_fbo.json']
files+=list((P/'ozon_recovery_20261103/daily_20261107/raw').glob('monitor_postings_*.json'))
files+=list(Path('/opt/mp-analytics/runtime/ozon_experiment_20261106/raw').glob('*_postings_*.json'))
files+=list(L.glob('monitoring_*/day0_refresh/postings_*.json'))
latest={}
for f in files:
 z=read(f)
 if not z.get('complete'):continue
 observed=z.get('captured_at_utc') or z.get('retrieved_at_utc');assert observed
 scheme=z.get('scheme') or f.stem.rsplit('_',1)[-1];assert scheme in ['fbs','fbo']
 for post in z['postings']:
  date=dt.datetime.fromisoformat((post.get('in_process_at') or post['created_at']).replace('Z','+00:00')).astimezone(TZ).date().isoformat()
  if not START<=date<=END:continue
  key=(scheme,post['posting_number'])
  if key not in latest or observed>latest[key][0]:latest[key]=(observed,post,date,str(f))
sales=[]
for (scheme,pn),(observed,post,date,source) in latest.items():
 if post['status'].lower().startswith('cancel'):continue
 for i,r in enumerate(post['products']):
  raw=str(r['sku']);sku=aliases.get(raw,raw);sales.append({'sku':sku,'model_id':models.get(raw,models.get(sku,'')),'date_msk':date,'order_id':str(post['order_id']),'ordered_value_rub':str(product_price(r)*number(r['quantity'])),'source':source,'observed_at_utc':observed})
csvout('private_general_sales_lines.csv',sales)
fixed={r['sku'] for r in rows(P/'ozon_recovery_20261103/revised_sku_summary_20.csv')};pool={r['sku'] for r in rows(L/'uploaded_sku_manifest.csv')};meta={str(c['id']):c for c in read(O/'live_campaigns.json')['response']['list']};live={cid for cid,c in meta.items() if c.get('advObjectType')=='SKU' and c['state']=='CAMPAIGN_STATE_RUNNING'};saved_placements=collections.defaultdict(set)
for f in C.glob('active_products_*.json'):
 z=read(f)
 for r in z['products']:saved_placements[aliases.get(str(r['sku']),str(r['sku']))].add(str(z['campaign_id']))
fresh={}
for it in read(O/'fresh_cards.json')['response']['items']:
 skus=[str(it.get('sku'))]+[str(r.get('sku')) for r in it.get('sources',[])];canonical=next((aliases[s] for s in skus if s in aliases),next((s for s in skus if s.isdigit()),''));fresh[canonical]=it
families_fixed={models.get(s) for s in fixed};wave1={r['sku'] for r in rows(L/'wave1_live_manifest.csv')};families_wave1={models.get(s) for s in wave1}
no_gain={r['sku']:r for r in rows(Y/'selling_before_cpc_no_observed_gain_review.csv')}
periods={'28d':START,'8d':RECENT};out=[]
for label,start in periods.items():
 sa=collections.defaultdict(lambda:[D(0),D(0),D(0),D(0)]);mo_cost=collections.defaultdict(lambda:D(0));values=collections.defaultdict(lambda:D(0));mo_value=collections.defaultdict(lambda:D(0));orders=collections.defaultdict(set);mo_orders=collections.defaultdict(set)
 for r in ad:
  if r['date_msk']<start:continue
  s=sa[r['sku']]
  for i,k in enumerate(['spend_rub','attributed_own_sales_rub','attributed_model_sales_rub','clicks']):s[i]+=number(r[k])
  if r['model_id']:mo_cost[r['model_id']]+=number(r['spend_rub'])
 for r in sales:
  if r['date_msk']<start:continue
  sku=r['sku'];m=r['model_id'];v=number(r['ordered_value_rub']);values[sku]+=v;orders[sku].add(r['order_id'])
  if m:mo_value[m]+=v;mo_orders[m].add(r['order_id'])
 for sku,(sp,own,halo,clicks) in sa.items():
  c=cat.get(sku,{})
  if sku in fixed or sku in pool or not (saved_placements[sku]&live):continue
  if c.get('own_has_stock')!='True' or c.get('archived')=='True':continue
  if sp<100:continue
  m=models.get(sku,c.get('model_id',''));row=next((r for r in out if r['sku']==sku),None)
  if row is None:
   row={'sku':sku,'article':c['offer_id'],'name':c['name'],'model_id':m,'current_campaign_ids':';'.join(sorted(saved_placements[sku]&live)),'current_strategy':';'.join(sorted({meta[cid].get('productAutopilotStrategy','UNKNOWN') for cid in saved_placements[sku]&live})),'fresh_card_checked':sku in fresh,'overlaps_fixed20_model':m in families_fixed,'overlaps_wave1_model':m in families_wave1,'historical_no_observed_gain_review':sku in no_gain};out.append(row)
  prefix=label+'_';row.update({prefix+'spend_rub':str(sp),prefix+'attributed_own_sales_rub':str(own),prefix+'attributed_model_sales_rub':str(halo),prefix+'drr_own_percent':str(sp/own*100) if own else '',prefix+'drr_with_model_percent':str(sp/(own+halo)*100) if own+halo else '',prefix+'clicks':str(clicks),prefix+'avg_cpc_rub':str(sp/clicks) if clicks else '',prefix+'own_unique_orders':len(orders[sku]),prefix+'own_ordered_value_rub':str(values[sku]),prefix+'model_unique_orders':len(mo_orders[m]),prefix+'model_ordered_value_rub':str(mo_value[m]),prefix+'model_cpc_rub':str(mo_cost[m]),prefix+'model_cpc_to_ordered_value_percent':str(mo_cost[m]/mo_value[m]*100) if mo_value[m] else ''})
out.sort(key=lambda r:D(r.get('28d_spend_rub','0')),reverse=True);save('all_screened_skus.json',out);source_summary={'period_28d':[START,END],'period_recent_8d':[RECENT,END],'returned_positive_placement_rows':len(ad),'unique_ad_skus':len({r['sku'] for r in ad}),'general_sales_lines':len(sales),'latest_postings':len(latest),'unknown_model_sales_lines':sum(not r['model_id'] for r in sales),'candidate_instock_legacy_skus_spend_ge100':len(out),'sources':dict(source_stats),'fixed20_excluded':len(fixed),'new_experiment_pool_excluded':len(pool),'marketplace_mutations':0,'production_database_writes':0};save('evidence_quality.json',source_summary)
print('Evidence:',json.dumps({k:v for k,v in source_summary.items() if k!='sources'},ensure_ascii=False))
for r in out:
 if D(r.get('28d_spend_rub','0'))>=500 and (r['current_strategy']=='MAX_CLICKS' or not r.get('28d_drr_with_model_percent') or D(r['28d_drr_with_model_percent'])>=15):print(json.dumps({k:r.get(k) for k in ['sku','article','current_strategy','28d_spend_rub','28d_avg_cpc_rub','28d_drr_with_model_percent','28d_own_unique_orders','28d_model_unique_orders','8d_spend_rub','8d_own_unique_orders','8d_model_unique_orders','overlaps_wave1_model']},ensure_ascii=False))

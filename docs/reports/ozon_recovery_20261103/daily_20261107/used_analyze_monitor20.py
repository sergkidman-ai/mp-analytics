# поток: mkt
"""Frozen-family versus cabinet dynamics, updated 2-Nov cancellation/shipping evidence."""
import sys,pathlib,json,csv,datetime as dt,decimal,collections,argparse
from zoneinfo import ZoneInfo
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price
P=pathlib.Path(__file__).parent;ROOT=P.parent;D=decimal.Decimal;TZ=ZoneInfo('Europe/Moscow')
parser=argparse.ArgumentParser(description='Analyze existing fixed-20 monitor snapshots without changing advertising or DB.')
parser.add_argument('--input',type=pathlib.Path,help='Dated output directory from fetch_monitor20.py')
a=parser.parse_args();O=a.input.resolve() if a.input else P;R=O/'raw'
config=json.loads((O/'monitor_config.json').read_text()) if a.input else None
start=config['refresh_from_msk'] if config else '2026-11-02';end=config['refresh_through_msk'] if config else '2026-11-02'
suffix=config['report_suffix'] if config else '20261102_03'
refreshed_days={(dt.date.fromisoformat(start)+dt.timedelta(days=i)).isoformat() for i in range((dt.date.fromisoformat(end)-dt.date.fromisoformat(start)).days+1)}
def read(p):
 with p.open() as f:return list(csv.DictReader(f))
def save(n,x):(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str)+'\n')
def write(n,rs):
 with (O/n).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
def utc(x):return dt.datetime.fromisoformat(x.replace('Z','+00:00')) if x else None
main=read(ROOT/'revised_sku_summary_20.csv');target={x['sku'] for x in main};models={x['current_family_model_id'] for x in main}
alias={};pmodel={}
for p in list((ROOT/'revision_raw').glob('product_info*json'))+list((P/'raw').glob('*_info_*.json'))+list(R.glob('monitor_unknown_products.json')):
 z=json.loads(p.read_text())
 for it in z.get('body',{}).get('items',[]):
  pid=str(it['id']);pmodel[pid]=str(it.get('model_info',{}).get('model_id'))
  for s in [it.get('sku')]+[x.get('sku') for x in it.get('sources',[])]:
   if s:alias[str(s)]=pid
postings=[];quality=[]
for scheme in ['fbs','fbo']:
 name=f'monitor_postings_{start.replace("-","")}_{end.replace("-","")}_{scheme}.json' if config else f'monitor_postings_20261102_{scheme}.json'
 z=json.loads((R/name).read_text());assert z['complete'],'Cannot analyze incomplete posting window';quality.append({k:v for k,v in z.items() if k!='postings'})
 for x in z.get('postings',[]):postings.append((scheme,z['retrieved_at_utc'],x))
unknown={str(p['sku']) for sc,at,x in postings for p in x['products'] if str(p['sku']) not in alias}
# Only exact missing 2-Nov posting products, if needed to classify cancellation evidence.
if unknown:
 from collectors.ozon import _headers,PRODUCT_INFO_URL
 import requests
 r=requests.post(PRODUCT_INFO_URL,headers=_headers('oz_acc1'),json={'sku':sorted(unknown)},timeout=90);r.raise_for_status()
 save('raw/monitor_unknown_products.json',{'http':r.status_code,'retrieved_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'requested_skus':sorted(unknown),'purpose':'only missing product model_id in refreshed post-window posting evidence','body':r.json()})
 with (O/'api_reads.jsonl').open('a') as f:f.write(json.dumps({'at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'method':'POST','path':'/v3/product/info/list','http':r.status_code,'body':{'sku':sorted(unknown)}})+'\n')
 for it in r.json().get('items',[]):
  pid=str(it['id']);pmodel[pid]=str(it.get('model_info',{}).get('model_id'))
  for s in [it.get('sku')]+[x.get('sku') for x in it.get('sources',[])]:
   if s:alias[str(s)]=pid
shipping=[];newlines=[]
postingkeys=set()
for scheme,stamp,x in postings:
 key=(scheme,x['posting_number']);assert key not in postingkeys,'Duplicate posting';postingkeys.add(key)
 when=utc(x.get('in_process_at') or x.get('created_at')).astimezone(TZ).date().isoformat();assert when in refreshed_days
 cancelled=x['status'].lower().startswith('cancel');skus={str(p['sku']) for p in x['products']};family=any(pmodel.get(alias.get(s)) in models for s in skus)
 shipment=utc(x.get('shipment_date'));without=utc(x.get('shipment_date_without_delay'));now=utc(stamp)
 overdue=x['status'] in ['awaiting_packaging','awaiting_deliver'] and shipment is not None and shipment<now
 row={'scheme':scheme,'posting_number':x['posting_number'],'order_id':str(x['order_id']),'status_asof_fetch':x['status'],'retrieved_at_utc':stamp,'in_process_at':x.get('in_process_at') or x.get('created_at'),'shipment_date':x.get('shipment_date'),'shipment_date_without_delay':x.get('shipment_date_without_delay'),'delivering_date':x.get('delivering_date'),'awaiting_and_shipment_time_elapsed':overdue,'shipment_date_later_than_without_delay':bool(shipment and without and shipment>without),'cancelled':cancelled,'cancel_reason':(x.get('cancellation') or {}).get('cancel_reason'),'cancel_initiator':(x.get('cancellation') or {}).get('cancellation_initiator'),'contains_fixed20_exact_sku':bool(skus&target),'contains_current_family':family,'unknown_product_model_count':sum(s not in alias for s in skus),'incident_causality':'not established'}
 row['date_msk']=when;shipping.append(row)
 if cancelled:continue
 for i,p in enumerate(x['products']):
  s=str(p['sku']);newlines.append({'date_msk':when,'scheme':scheme,'posting_number':x['posting_number'],'order_id':str(x['order_id']),'sku':s,'current_model_id':pmodel.get(alias.get(s),''),'quantity':p['quantity'],'ordered_value_rub':product_price(p)*p['quantity'],'source':'raw/monitor_postings_'+start.replace('-','')+'_'+end.replace('-','')+'_'+scheme+'.json','line_index':i})
write('monitor20_shipping.csv' if config else 'monitor20_shipping_20261102.csv',shipping)
# Replace date 2-Nov only. Never append duplicate old/new observations as extra sales.
lines=[r for r in read(ROOT/'revised_sales_lines.csv') if r['date_msk'] not in refreshed_days]+newlines
for x in lines:x['ordered_value_rub']=D(str(x['ordered_value_rub']))
base28=[(dt.date(2026,10,2)+dt.timedelta(days=i)).isoformat() for i in range(28)]
post=[(dt.date(2026,10,31)+dt.timedelta(days=i)).isoformat() for i in range((dt.date.fromisoformat(end)-dt.date(2026,10,31)).days+1)]
def measures(rr):return {'orders':D(len({x['order_id'] for x in rr})),'ordered_value_rub':sum((x['ordered_value_rub'] for x in rr),D(0))}
def select(scope,days):return [x for x in lines if x['date_msk'] in days and (scope=='cabinet' or (scope=='families_union' and x['current_model_id'] in models) or (scope=='fixed20' and x['sku'] in target))]
rr=[];lookup={}
for n,base in [(14,base28[-14:]),(28,base28)]:
 for scope in ['fixed20','families_union','cabinet']:
  actual=measures(select(scope,post));expected={m:sum((sum((measures(select(scope,[b]))[m] for b in base if dt.date.fromisoformat(b).weekday()==dt.date.fromisoformat(d).weekday()),D(0))/D(n//7) for d in post),D(0)) for m in actual}
  for metric in actual:
   g=actual[metric]/expected[metric]-1 if expected[metric] else None
   x={'scope':scope,'baseline_days':n,'metric':metric,'actual_post':actual[metric],'expected_same_weekdays':expected[metric],'delta':actual[metric]-expected[metric],'change_percent':g*100 if g is not None else None,'post_days':len(post),'composition':'same frozen 19 current model_id groups on both sides; no historical composition claim'}
   rr.append(x);lookup[(scope,n,metric)]=x
relative=[]
for n in [14,28]:
 for metric in ['orders','ordered_value_rub']:
  fam=lookup[('families_union',n,metric)];cab=lookup[('cabinet',n,metric)]
  relative.append({'baseline_days':n,'metric':metric,'family_actual':fam['actual_post'],'family_expected':fam['expected_same_weekdays'],'family_change_percent':fam['change_percent'],'cabinet_actual':cab['actual_post'],'cabinet_expected':cab['expected_same_weekdays'],'cabinet_change_percent':cab['change_percent'],'difference_percentage_points':fam['change_percent']-cab['change_percent'],'relative_growth_factor':(fam['actual_post']/fam['expected_same_weekdays'])/(cab['actual_post']/cab['expected_same_weekdays']) if fam['expected_same_weekdays'] and cab['actual_post'] and cab['expected_same_weekdays'] else None,'interpretation':'descriptive dynamics, not adjusted causal effect'})
write('monitor20_family_vs_cabinet.csv',relative);write('monitor20_sales_comparison.csv',rr)
summary=[]
for scope,pred in [('cabinet',lambda x:True),('fixed20',lambda x:x['contains_fixed20_exact_sku']),('families_union',lambda x:x['contains_current_family'])]:
 aa=[x for x in shipping if pred(x)];summary.append({'scope':scope,'postings':len(aa),'unique_order_ids_including_cancelled':len({x['order_id'] for x in aa}),'cancelled_postings':sum(x['cancelled'] for x in aa),'awaiting_ship_time_elapsed':sum(x['awaiting_and_shipment_time_elapsed'] for x in aa),'later_shipment_date_field':sum(x['shipment_date_later_than_without_delay'] for x in aa),'cancel_initiators':dict(collections.Counter(str(x['cancel_initiator']) for x in aa if x['cancelled'])),'cancel_reasons':dict(collections.Counter(str(x['cancel_reason']) for x in aa if x['cancelled']))})
save('monitor20_shipping_summary.json',{'snapshots':quality,'scopes':summary,'unknown_product_model_lines':sum(x['unknown_product_model_count'] for x in shipping),'interpretation':'elapsed planned shipment time while awaiting is an available delay signal, not proof of incident cause. Later shipment_date versus without_delay is not itself proof of operational delay. Cancellation reasons preserved, no incident attribution.'})
# Preserve fresh 20-SKU advertising observations and label partial current day.
folder=R/f'monitor20_{suffix}';adrows=[]
if folder.exists():
 for p in folder.glob('*.csv'):
  cid=p.name.split('_')[0]
  with p.open(encoding='utf-8-sig',newline='') as f:
   next(f);reader=csv.DictReader(f,delimiter=';')
   for r in reader:
    s=r.get('sku')
    if s not in target:continue
    day=dt.datetime.strptime(r['День'],'%d.%m.%Y').date().isoformat()
    def num(x):return D(str(x).replace(' ','').replace('\xa0','').replace(',','.'))
    adrows.append({'sku':s,'campaign_id':cid,'date_msk':day,'views':num(r['Показы']),'clicks':num(r['Клики']),'spend_rub':num(r['Расход, ₽, с НДС']),'ad_orders_api':num(r['Продано товаров']),'ad_revenue_rub':num(r['Продажи в продвижении, ₽']),'partial_day':day>=dt.datetime.now(TZ).date().isoformat() if config else day=='2026-11-03','source':str(p.relative_to(O))})
 if adrows:write('monitor20_ad_fresh.csv',adrows)
assert len(adrows)==len({(r['sku'],r['campaign_id'],r['date_msk']) for r in adrows}),'Duplicate advertising rows'
daily=[]
for day in post:
 ad=[r for r in adrows if r['date_msk']==day]
 row={'date_msk':day,'explicit_ad_skus':len({r['sku'] for r in ad}),'explicit_ad_placements':len(ad),'spend_rub':sum((r['spend_rub'] for r in ad),D(0)) if ad else None,'ad_orders_api':sum((r['ad_orders_api'] for r in ad),D(0)) if ad else None}
 for scope in ['fixed20','families_union','cabinet']:
  row.update({scope+'_'+k:v for k,v in measures(select(scope,[day])).items()})
 daily.append(row)
write('monitor20_daily.csv',daily)
metrics=['views','clicks','spend_rub','ad_orders_api','ad_revenue_rub']
oldads=read(ROOT/'revised_sku_campaign_daily.csv')
for r in oldads:
 for m in metrics:r[m]=D(r[m])
allads=[r for r in oldads if r['date_msk'] not in refreshed_days]+adrows
idx={(r['sku'],r['campaign_id'],r['date_msk']):r for r in allads}
assert len(idx)==len(allads),'Duplicate merged ad observations'
adcompare=[];paircoverage=[]
for n,base in [(14,base28[-14:]),(28,base28)]:
 refs={d:[b for b in base if dt.date.fromisoformat(b).weekday()==dt.date.fromisoformat(d).weekday()] for d in post}
 required=set(post+sum(refs.values(),[]));pairs={(r['sku'],r['campaign_id']) for r in allads}
 comparable={pair for pair in pairs if all((*pair,d) in idx for d in required)}
 paircoverage.append({'baseline_days':n,'comparable_placements':len(comparable),'covered_skus':len({s for s,c in comparable}),'required_days':len(required),'missing_is_zero':False})
 for m in metrics:
  actual=sum((idx[(s,c,d)][m] for s,c in comparable for d in post),D(0))
  expected=sum((sum((idx[(s,c,b)][m] for b in refs[d]),D(0))/D(n//7) for s,c in comparable for d in post),D(0))
  adcompare.append({'baseline_days':n,'metric':m,'comparable_placements':len(comparable),'covered_skus':len({s for s,c in comparable}),'actual_post':actual,'expected_same_weekdays':expected,'delta':actual-expected,'change_percent':(actual/expected-1)*100 if expected else None,'causal_claim_allowed':False})
write('monitor20_ad_comparison.csv',adcompare)
save('monitor20_ad_pair_coverage.json',paircoverage)
per_sku=[]
for sku in sorted(target):
 ad=[r for r in adrows if r['sku']==sku];sales=[r for r in lines if r['sku']==sku and r['date_msk'] in post]
 per_sku.append({'sku':sku,'post_days':len(post),'explicit_ad_days':len({r['date_msk'] for r in ad}),'spend_rub':sum((r['spend_rub'] for r in ad),D(0)) if ad else None,'ad_orders_api':sum((r['ad_orders_api'] for r in ad),D(0)) if ad else None,**measures(sales)})
write('monitor20_sku_summary_20.csv',per_sku)
# Refresh old post dates rather than treating attribution/cancellations as new-day sales.
revisions=[]
oldlines=read(ROOT/'revised_sales_lines.csv')
for r in oldlines:r['ordered_value_rub']=D(r['ordered_value_rub'])
for day in sorted(refreshed_days&{'2026-10-31','2026-11-01','2026-11-02'}):
 for scope,pred in [('fixed20',lambda r:r['sku'] in target),('families_union',lambda r:r['current_model_id'] in models)]:
  before=measures([r for r in oldlines if r['date_msk']==day and pred(r)]);after=measures(select(scope,[day]))
  for metric in before:revisions.append({'date_msk':day,'scope':scope,'metric':metric,'previous_snapshot':before[metric],'refreshed_snapshot':after[metric],'delta':after[metric]-before[metric]})
 for metric in metrics:
  before=sum((r[metric] for r in oldads if r['date_msk']==day),D(0));after=sum((r[metric] for r in adrows if r['date_msk']==day),D(0))
  revisions.append({'date_msk':day,'scope':'fixed20_ads_all_explicit_placements','metric':metric,'previous_snapshot':before,'refreshed_snapshot':after,'delta':after-before})
if revisions:write('monitor20_late_revisions.csv',revisions)
save('monitor20_quality.json',{'post_days':post,'refreshed_post_days':sorted(refreshed_days),'fixed_sku_count':len(target),'frozen_family_count':len(models),'posting_snapshots_complete':all(x['complete'] for x in quality),'unknown_model_sales_lines':sum(not r['current_model_id'] for r in newlines),'ad_explicit_rows':len(adrows),'ad_sku_day_coverage':len({(r['sku'],r['date_msk']) for r in adrows}),'ad_sku_day_expected':len(target)*len(post),'missing_rows_are_zero':False,'late_attribution_final':False,'post_refresh_replaces_old_rows':True})
print('Analyzed complete posting snapshots:',len(quality),'fresh noncancelled product lines:',len(newlines),flush=True)
print('Refreshed advertising rows:',len(adrows),'post days:',len(post),'unknown sales model lines:',sum(not r['current_model_id'] for r in newlines),flush=True)

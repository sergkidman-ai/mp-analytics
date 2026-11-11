# поток: mkt / ozon
"""Complete annual comparisons from finished normalized layers, one SKU at a time."""
import collections
import csv
import datetime as dt
import decimal
import hashlib
import itertools
import json
import math
import pathlib
import sys
from zoneinfo import ZoneInfo

sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price

OUT=pathlib.Path(__file__).parent;CAT=OUT.parent/'ozon_full_inventory_20261105'
START,END=dt.date(2025,11,5),dt.date(2026,11,4);TZ=ZoneInfo('Europe/Moscow');D=decimal.Decimal
cache={}
def day(value):
    if value not in cache:
        try:cache[value]=dt.datetime.strptime(value,'%d.%m.%Y').date()
        except ValueError:
            try:cache[value]=dt.datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(TZ).date()
            except (ValueError,OverflowError):cache[value]=None
    return cache[value]
def num(value):
    try:return float(str(value or '0').replace(' ','').replace('\xa0','').replace(',','.'))
    except ValueError:return 0.0
def cents(value):return int(round(num(value)*100))
def save(name,z):(OUT/name).write_text(json.dumps(z,ensure_ascii=False,indent=2)+'\n')
def write(name,rows,fields):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def logpmf(k,n,p):
    if p<=0:return 0.0 if k==0 else -math.inf
    if p>=1:return 0.0 if k==n else -math.inf
    return math.lgamma(n+1)-math.lgamma(k+1)-math.lgamma(n-k+1)+k*math.log(p)+(n-k)*math.log1p(-p)
def cdf(n,k,p):
    if k<0:return 0.0
    if k>=n:return 1.0
    vv=[logpmf(i,n,p) for i in range(k+1)];m=max(vv)
    return 0.0 if not math.isfinite(m) else min(1.0,math.exp(m)*sum(math.exp(v-m) for v in vv))
def exact_p(k,n,p):
    if not n:return 1.0
    bound=logpmf(k,n,p)+1e-10;vv=[logpmf(i,n,p) for i in range(n+1) if logpmf(i,n,p)<=bound]
    m=max(vv);return min(1.0,math.exp(m)*sum(math.exp(v-m) for v in vv))
def interval(k,n,alpha=.10):
    if not n:return 0.0,1.0
    lower=0.0
    if k:
        lo,hi=0.,1.
        for _ in range(40):
            mid=(lo+hi)/2
            if 1-cdf(n,k-1,mid)<alpha/2:lo=mid
            else:hi=mid
        lower=(lo+hi)/2
    upper=1.0
    if k<n:
        lo,hi=0.,1.
        for _ in range(40):
            mid=(lo+hi)/2
            if cdf(n,k,mid)>alpha/2:lo=mid
            else:hi=mid
        upper=(lo+hi)/2
    return lower,upper
assert abs(exact_p(0,10,.5)-.001953125)<1e-10
assert abs(interval(0,10,.05)[1]-(1-.025**.1))<1e-8

status=json.loads((OUT/'sku_collection_status.json').read_text());assert status['complete']==status['jobs_expected']==16
normalized=OUT/'sku_positive_cpc_daily_year.csv'
assert normalized.is_file() and (OUT/'account_general_sales_daily_year.csv').is_file()
with (OUT/'account_general_sales_daily_year.csv').open() as f:assert sum(1 for _ in csv.DictReader(f))==365
catalog={};alias={}
for f in sorted((CAT/'raw').glob('info_*.json')):
    z=json.loads(f.read_text())
    for x in z['response']['items']:
        sku=str(x.get('sku') or '')
        if not sku:continue
        stocks=x.get('stocks') or {};quantity=sum(max(0,num(s.get('present'))-num(s.get('reserved'))) for s in stocks.get('stocks',[]))
        catalog[sku]={'offer_id':x.get('offer_id',''),'name':x.get('name',''),'model_id':str((x.get('model_info') or {}).get('model_id') or ''),
                      'created_day':day(x.get('created_at','')),'currently_in_stock':bool(stocks.get('has_stock') and quantity>0 and not x.get('is_archived') and not x.get('is_autoarchived'))}
        for s in [x.get('sku')]+[a.get('sku') for a in x.get('sources',[])]:
            if s:alias[str(s)]=sku
del z

orders=collections.defaultdict(set);values=collections.defaultdict(int);account_orders=collections.defaultdict(set);annual=collections.defaultdict(lambda:{'orders':set(),'value':0})
seen=set();covered={s:set() for s in ['fbs','fbo']};post_counts=collections.Counter()
for w in json.loads((OUT/'order_collection_coverage.json').read_text())['windows']:
    z=json.loads(pathlib.Path(w['path']).read_text());assert z.get('complete')
    a,b=dt.date.fromisoformat(w['from_msk']),dt.date.fromisoformat(w['through_msk'])
    covered[w['scheme']].update(a+dt.timedelta(days=i) for i in range((b-a).days+1))
    for r in z['postings']:
        key=(w['scheme'],r['posting_number']);assert key not in seen;seen.add(key);post_counts['postings']+=1
        if r['status'].lower().startswith('cancel'):post_counts['cancelled_postings']+=1;continue
        d=dt.datetime.fromisoformat((r.get('in_process_at') or r.get('created_at')).replace('Z','+00:00')).astimezone(TZ).date()
        if not START<=d<=END:continue
        oid=str(r['order_id']);account_orders[d].add(oid)
        for it in r['products']:
            sku=alias.get(str(it['sku']),str(it['sku']));k=(sku,d);orders[k].add(oid)
            value=int((product_price(it)*D(str(it['quantity']))*100).to_integral_value());values[k]+=value
            annual[sku]['orders'].add(oid);annual[sku]['value']+=value
assert all(len(v)==365 for v in covered.values())
del z,seen

expected_daily=collections.defaultdict(lambda:[0,0])
with (OUT/'campaign_daily_year.csv').open(encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        if r['type']=='SKU' and r['payment_type']=='CPC':
            expected_daily[day(r['date_msk'])][0]+=int(num(r['views']));expected_daily[day(r['date_msk'])][1]+=cents(r['spend_rub'])
observed_daily=collections.defaultdict(lambda:[0,0]);ad_skus=set();row_count=0;previous_key=None
with normalized.open(encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        k=(r['sku'],r['date_msk']);assert previous_key is None or previous_key<k;previous_key=k;row_count+=1
        ad_skus.add(r['sku']);d=day(r['date_msk']);observed_daily[d][0]+=int(num(r['views']));observed_daily[d][1]+=cents(r['spend_rub'])
bad_days=set();reconciliation=[]
for d in sorted(set(expected_daily)|set(observed_daily)):
    e,o=expected_daily[d],observed_daily[d]
    missing=(e[0]>0 and o[0]==0) or (e[1]>100 and o[1]==0)
    material=abs(e[1]-o[1])>max(100,int(abs(e[1])*.05))
    if missing or material:bad_days.add(d)
    if missing or abs(e[1]-o[1])>100:reconciliation.append({'date_msk':str(d),'campaign_views':e[0],'sku_views':o[0],'campaign_spend_rub':e[1]/100,'sku_spend_rub':o[1]/100,'missing':missing,'material_cost_difference':material})
save('ad_daily_reconciliation.json',reconciliation)
save('normalization_checkpoint.json',{'rows':row_count,'positive_cpc_skus':len(ad_skus),'sha256':hashlib.sha256(normalized.read_bytes()).hexdigest(),'account_days':365,'normalized_layers_completed_before_first_process_exit':True,'streaming_resume':True})

e8=set()
with (OUT.parent.parent/'experiments/cohorts/E8_treatment_2026-08-20.csv').open() as f:
    for r in csv.DictReader(f):
        if r.get('account')=='oz_acc1':e8.add(alias.get(r['sku'],r['sku']))
def count_sku(sku,dates):return len(set().union(*(orders.get((sku,d),set()) for d in dates)))
def count_account(dates):return len(set().union(*(account_orders[d] for d in dates)))
events=[];processed=0;annual_cpc_cost={};annual_attributed_sales={};annual_traffic_days={};after_union_cost={}
with normalized.open(encoding='utf-8-sig') as f:
    for sku,iterator in itertools.groupby(csv.DictReader(f),key=lambda r:r['sku']):
        rr=list(iterator);daily={day(r['date_msk']):r for r in rr};dates=set(daily);last=None;meta=catalog.get(sku,{});event_start=len(events)
        annual_cpc_cost[sku]=sum(cents(r['spend_rub']) for r in rr)
        annual_attributed_sales[sku]=sum(cents(r['attributed_sales_rub']) for r in rr)
        annual_traffic_days[sku]=len(dates)
        for first in sorted(dates):
            off=(first-last).days-1 if last else (first-START).days
            if off<7:last=first;continue
            n=28 if off>=28 else 14 if off>=14 else 7
            if first+dt.timedelta(days=n-1)>END:last=first;continue
            pre=[first-dt.timedelta(days=i) for i in range(n,0,-1)];post=[first+dt.timedelta(days=i) for i in range(n)]
            nb,na=count_sku(sku,pre),count_sku(sku,post);ab,aa=count_account(pre),count_account(post)
            if not ab or not aa:last=first;continue
            bg=aa/ab;low,high=interval(na,na+nb)
            lowrr=(low/(1-low))/bg if low<1 else None;highrr=(high/(1-high))/bg if high<1 else None
            created=meta.get('created_day');identity=bool(created and created>first);immature=bool(created and pre[0]<created<=first)
            before28=nb*28/n;after28=na*28/n;exposed=sum(d in dates for d in post)
            spend=sum(cents(daily[d]['spend_rub']) for d in post if d in daily)
            source='HISTORICAL_E8_RESTORE_PLUS_OBSERVED_TRAFFIC' if sku in e8 and dt.date(2026,8,19)<=first<=dt.date(2026,8,25) else 'OBSERVED_CPC_TRAFFIC_RESUMPTION_CONFIG_UNKNOWN'
            events.append({'sku':sku,'offer_id':meta.get('offer_id',''),'name':meta.get('name',''),'currently_in_stock':bool(meta.get('currently_in_stock')),
                           'model_id_now':meta.get('model_id',''),'first_positive_cpc_day':str(first),'window_days':n,'before_from':str(pre[0]),'before_to':str(pre[-1]),'after_to':str(post[-1]),
                           'orders_before':nb,'orders_after':na,'orders_before_per28':round(before28,4),'orders_after_per28':round(after28,4),
                           'ordered_value_before_rub':sum(values.get((sku,d),0) for d in pre)/100,'ordered_value_after_rub':sum(values.get((sku,d),0) for d in post)/100,
                           'cpc_spend_after_rub':spend/100,'cpc_traffic_days_after':exposed,'account_order_rate_factor':round(bg,6),
                           'relative_order_rate_factor':na/nb/bg if nb else None,'rate_ratio_90ci_lower':lowrr,'rate_ratio_90ci_upper':highrr,
                           'p_value':exact_p(na,na+nb,bg/(1+bg)),'q_value':None,
                           'demand_before':'rare_0_2_per_28d' if before28<=2 else 'frequent_8plus_per_28d' if before28>=8 else 'middle_2_8_per_28d',
                           'rare_in_both_windows':before28<=2 and after28<=2,'event_source':source,
                           'creation_date_after_historical_exposure':identity,'new_card_without_full_pre_window':immature,
                           'ad_coverage_issue_in_window':any(d in bad_days for d in pre+post),'historical_stock_known':False,
                           'CPO_historical_off_proven':False,'causal_advertising_effect_proven':False,'effect_class':'PENDING'})
            last=first
        union_days=set()
        for event in events[event_start:]:
            first=dt.date.fromisoformat(event['first_positive_cpc_day'])
            union_days.update(first+dt.timedelta(days=i) for i in range(event['window_days']))
        after_union_cost[sku]=sum(cents(daily[d]['spend_rub']) for d in union_days if d in daily)
        processed+=1
        if processed%1000==0:print('Streaming SKU',processed,'candidate episodes',len(events),flush=True)

running=1.;m=len(events)
for rank,e in reversed(list(enumerate(sorted(events,key=lambda e:e['p_value']),1))):
    running=min(running,e['p_value']*m/rank);e['q_value']=min(1.,running)
for e in events:
    compromised=e['creation_date_after_historical_exposure'] or e['new_card_without_full_pre_window'] or e['ad_coverage_issue_in_window']
    enough=e['cpc_traffic_days_after']>=min(7,e['window_days'])
    if compromised:e['effect_class']='INSUFFICIENT_BASELINE_OR_IDENTITY_OR_COVERAGE'
    elif e['rare_in_both_windows']:e['effect_class']='RARE_BOTH_MODES_EFFECT_UNPROVEN'
    elif enough and e['q_value']<.05 and e['rate_ratio_90ci_lower'] is not None and e['rate_ratio_90ci_lower']>1.2:e['effect_class']='OBSERVED_GROWTH_AFTER_CPC_NOT_CAUSAL_PROOF'
    elif enough and e['q_value']<.05 and e['rate_ratio_90ci_upper'] is not None and e['rate_ratio_90ci_upper']<.8:e['effect_class']='OBSERVED_DECLINE_AFTER_CPC_NOT_CAUSAL_PROOF'
    elif enough and e['rate_ratio_90ci_lower'] is not None and e['rate_ratio_90ci_upper'] is not None and .8<=e['rate_ratio_90ci_lower']<=e['rate_ratio_90ci_upper']<=1.2:e['effect_class']='NO_MATERIAL_OBSERVED_DIFFERENCE_WITHIN_20PCT'
    else:e['effect_class']='INSUFFICIENT_PRECISION_OR_EXPOSURE'
events.sort(key=lambda e:(e['sku'],e['first_positive_cpc_day']));fields=list(events[0]) if events else ['sku','effect_class']
write('transition_episodes_all.csv',events,fields);stock_events=[e for e in events if e['currently_in_stock']];write('transition_episodes_currently_in_stock.csv',stock_events,fields)
by_sku=collections.defaultdict(list)
for e in stock_events:by_sku[e['sku']].append(e)
sku_rows=[]
for sku,ee in by_sku.items():
    classes={e['effect_class'] for e in ee};meta=catalog[sku]
    if 'OBSERVED_GROWTH_AFTER_CPC_NOT_CAUSAL_PROOF' in classes and 'OBSERVED_DECLINE_AFTER_CPC_NOT_CAUSAL_PROOF' in classes:category='MIXED_RESPONSE_OVER_YEAR'
    elif 'OBSERVED_GROWTH_AFTER_CPC_NOT_CAUSAL_PROOF' in classes:category='OBSERVED_GROWTH_NOT_CAUSAL_PROOF'
    elif 'NO_MATERIAL_OBSERVED_DIFFERENCE_WITHIN_20PCT' in classes:category='NO_MATERIAL_OBSERVED_DIFFERENCE'
    elif classes=={'RARE_BOTH_MODES_EFFECT_UNPROVEN'}:category='RARE_BOTH_MODES_EFFECT_UNPROVEN'
    else:category='INSUFFICIENT_OR_CONFOUNDED'
    sku_rows.append({'sku':sku,'offer_id':meta['offer_id'],'name':meta['name'],'currently_in_stock':True,
                     'observed_transition_episodes':len(ee),'preliminary_category':category,
                     'annual_general_orders':len(annual[sku]['orders']),'annual_ordered_value_rub':annual[sku]['value']/100,
                     'demand_before_labels':';'.join(sorted({e['demand_before'] for e in ee})),
                     'rare_both_modes_episodes':sum(e['rare_in_both_windows'] for e in ee),
                     'unique_after_window_cpc_spend_rub':after_union_cost.get(sku,0)/100,
                     'annual_cpc_spend_rub':annual_cpc_cost.get(sku,0)/100,
                     'causal_advertising_effect_proven':False,'historical_stock_known':False,'off_means_no_observed_CPC_traffic_not_all_ads':True})
sku_rows.sort(key=lambda r:(r['preliminary_category'],-r['annual_cpc_spend_rub']))
write('sku_transition_summary_in_stock.csv',sku_rows,list(sku_rows[0]) if sku_rows else ['sku','preliminary_category'])
never=[]
for sku,s in annual.items():
    meta=catalog.get(sku,{})
    if meta.get('currently_in_stock') and sku not in ad_skus:never.append({'sku':sku,'offer_id':meta['offer_id'],'name':meta['name'],'annual_general_orders':len(s['orders']),'annual_ordered_value_rub':s['value']/100,'CPO_off_not_proven':True,'current_stock_confirmed':True})
write('selling_without_observed_cpc_in_stock.csv',sorted(never,key=lambda r:-r['annual_general_orders']),list(never[0]) if never else ['sku'])
profile=[]
for sku,meta in catalog.items():
    if not meta['currently_in_stock']:continue
    general=annual.get(sku,{'orders':set(),'value':0});cost=annual_cpc_cost.get(sku,0);attrib=annual_attributed_sales.get(sku,0)
    profile.append({'sku':sku,'offer_id':meta['offer_id'],'name':meta['name'],'currently_in_stock':True,
                    'annual_general_orders':len(general['orders']),'annual_ordered_value_rub':general['value']/100,
                    'annual_cpc_spend_rub':cost/100,'annual_attributed_sales_rub':attrib/100,
                    'annual_drr_attribution_percent':cost/attrib*100 if attrib else None,
                    'annual_cpc_spend_to_all_ordered_value_percent':cost/general['value']*100 if general['value'] else None,
                    'days_with_positive_cpc_traffic':annual_traffic_days.get(sku,0),'has_observed_off_on_comparison':sku in by_sku,
                    'causal_effect_proven':False,'CPO_historical_off_proven':False})
profile.sort(key=lambda r:-r['annual_cpc_spend_rub'])
write('annual_sku_profile_currently_in_stock.csv',profile,list(profile[0]))
write('no_usable_off_on_comparison_in_stock.csv',[r for r in profile if r['days_with_positive_cpc_traffic'] and not r['has_observed_off_on_comparison']],list(profile[0]))
quality=json.loads((OUT/'date_added_quality.json').read_text()) if (OUT/'date_added_quality.json').exists() else {}
summary={'account':'oz_acc1','period':[str(START),str(END)],'order_days_complete':{s:len(v) for s,v in covered.items()},'posting_counts':dict(post_counts),
         'general_sales_skus':len(annual),'positive_cpc_skus':len(ad_skus),'sparse_positive_sku_days':row_count,
         'report_row_quality':quality.get('counts',{}),'affected_ad_coverage_days':len(bad_days),
         'total_candidate_episodes':len(events),'currently_in_stock_candidate_episodes':len(stock_events),'currently_in_stock_skus_with_episodes':len(sku_rows),
         'episode_classes_in_stock':dict(collections.Counter(e['effect_class'] for e in stock_events)),
         'sku_categories_in_stock':dict(collections.Counter(r['preliminary_category'] for r in sku_rows)),
         'selling_without_observed_cpc_in_stock_skus':len(never),'current_stock_annual_profile_skus':len(profile),
         'current_stock_advertised_without_usable_off_on_comparison':sum(r['days_with_positive_cpc_traffic']>0 and not r['has_observed_off_on_comparison'] for r in profile),
         'rare_both_modes_annual_CPC_spend_rub':round(sum(r['annual_cpc_spend_rub'] for r in sku_rows if r['preliminary_category']=='RARE_BOTH_MODES_EFFECT_UNPROVEN'),2),
         'causal_growth_or_no_effect_claims_allowed':False,
         'no_ad_definition':'no positive CPC traffic/cost observed in retained known campaign reports; not proof of disabled configuration or CPO absence',
         'business_thresholds_draft':{'rare_orders_per28_max':2,'frequent_orders_per28_min':8,'no_material_difference_band':[.8,1.2],'FDR_q':.05},
         'historical_stock_not_recovered':True,'deleted_campaign_completeness_not_proven':True,'month_experiment_needed_for_prospective_validation':True,
         'streaming_analysis':True,'marketplace_mutations':0,'production_database_writes':0}
save('analysis_summary.json',summary)
print('ANNUAL STREAMING ANALYSIS COMPLETE',summary,flush=True)

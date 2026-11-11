# поток: mkt
"""Offline correction: 20 unique SKU, two baselines, placement and sales scopes separated."""
import pathlib,json,csv,datetime as dt,decimal,collections,hashlib
from zoneinfo import ZoneInfo
O=pathlib.Path(__file__).parent;R=O/'revision_raw';D=decimal.Decimal
TARGET='1510947813 863386191 1719404303 1277712843 930627436 2909045114 930623456 1658970308 866442961 1999321262 3557209622 906874407 4504239311 4518308193 652626682 4463532700 4480826404 4518304506 4489633315 4518302691'.split()
def load(n):return json.loads((O/n).read_text())
def read(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def write(n,rs):
 with (O/n).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
def num(x):return D(str(x).replace(',','.').replace(' ','')) if x not in ['',None] else None
def ratio(a,b,scale=1):return a/b*D(scale) if a is not None and b else None
def day(s):
 for fmt in ['%Y-%m-%d','%d.%m.%Y']:
  try:return dt.datetime.strptime(s,fmt).date().isoformat()
  except ValueError:pass
 raise ValueError(s)
metrics=['views','clicks','spend_rub','ad_orders_api','ad_revenue_rub']
post=['2026-10-31','2026-11-01','2026-11-02'];base28=[(dt.date(2026,10,2)+dt.timedelta(days=i)).isoformat() for i in range(28)];bases={14:base28[-14:],28:base28}
rows=read('sku_campaign_daily.csv');assert len(rows)==len({(x['sku'],x['campaign_id'],x['date_msk']) for x in rows})
for r in rows:
 for m in metrics:r[m]=num(r[m])
index={(r['sku'],r['campaign_id'],r['date_msk']):r for r in rows}
write('revised_sku_campaign_daily.csv',rows)
bids=read('current_bids.csv');pairs={(r['sku'],r['campaign_id']) for r in bids}|{(r['sku'],r['campaign_id']) for r in rows}
anyday={}
for s in TARGET:
 for date in base28+['2026-10-30']+post:
  rr=[r for r in rows if r['sku']==s and r['date_msk']==date]
  if rr:anyday[(s,date)]={m:sum((r[m] for r in rr),D(0)) for m in metrics}
reference={n:{d:[b for b in base if dt.date.fromisoformat(b).weekday()==dt.date.fromisoformat(d).weekday()] for d in post} for n,base in bases.items()}
cohorts={};exclusions=[]
for n,base in bases.items():
 need=set(post+sum(reference[n].values(),[]));cohorts[n]=set()
 for s,c in sorted(pairs):
  missing=[d for d in sorted(need) if (s,c,d) not in index]
  if not missing:cohorts[n].add((s,c))
  else:
   observed=[index[(s,c,d)] for d in post if (s,c,d) in index]
   refs=sorted(set(sum(reference[n].values(),[])))
   exclusions.append({'baseline_days':n,'sku':s,'campaign_id':c,'comparison_required_dates':len(need),'missing_required_dates':';'.join(missing),'post_days_reported':len(observed),'post_days_expected':3,'reference_days_reported':sum((s,c,d) in index for d in refs),'reference_days_expected':len(refs),'reported_post_spend_rub':sum((x['spend_rub'] for x in observed),D(0)) if observed else None,'reason':'пустой/неполный ряд; отсутствующая строка не равна нулю'})
write('revised_pair_exclusions.csv',exclusions)
def series(s,scope,n):
 if scope=='all_available':return {d:anyday[(s,d)] for d in bases[n]+post if (s,d) in anyday}
 pp={c for ss,c in cohorts[n] if ss==s};out={}
 for d in bases[n]+post:
  if pp and all((s,c,d) in index for c in pp):out[d]={m:sum((index[(s,c,d)][m] for c in pp),D(0)) for m in metrics}
 return out
comparisons=[];per={}
for n,base in bases.items():
 for scope in ['constant_pairs','all_available']:
  for s in TARGET:
   ss=series(s,scope,n);need=set(post+sum(reference[n].values(),[]));complete=need<=set(ss)
   out={'sku':s,'scope':scope,'baseline_days':n,'base_days_observed':sum(d in ss for d in base),'post_days_observed':sum(d in ss for d in post),'same_weekday_reference_days':len(set(sum(reference[n].values(),[]))),'reference_days_per_weekday':n//7,'comparison_complete_on_selected_dates':complete,'placement_pairs':sum(sss==s for sss,c in cohorts[n]) if scope=='constant_pairs' else sum(sss==s for sss,c in pairs)}
   for m in metrics:
    bt=sum((ss[d][m] for d in base if d in ss),D(0));pt=sum((ss[d][m] for d in post if d in ss),D(0))
    ex=sum((sum((ss[b][m] for b in reference[n][d]),D(0))/D(n//7) for d in post),D(0)) if complete else None
    out['base_reported_total_'+m]=bt
    out['base_mean_observed_day_'+m]=bt/D(out['base_days_observed']) if out['base_days_observed'] else None
    out['base_full_window_mean_'+m]=bt/D(n) if out['base_days_observed']==n else None
    out['post_total_'+m]=pt if out['post_days_observed']==3 else None
    out['post_daily_'+m]=pt/D(3) if out['post_days_observed']==3 else None
    out['expected_post_'+m]=ex;out['expected_daily_'+m]=ex/D(3) if ex is not None else None
    out['delta_'+m]=pt-ex if ex is not None and out['post_days_observed']==3 else None
   for pref in ['base_reported_total_','post_total_','expected_post_']:
    out[pref+'cpc_rub']=ratio(out[pref+'spend_rub'],out[pref+'clicks'])
    out[pref+'drr_percent']=ratio(out[pref+'spend_rub'],out[pref+'ad_revenue_rub'],100)
   out['spend_delta_percent']=ratio(out['delta_spend_rub'],out['expected_post_spend_rub'],100)
   comparisons.append(out);per[(s,scope,n)]=out
write('revised_ad_comparison_by_sku.csv',comparisons)
summary=[]
for n in bases:
 for scope in ['constant_pairs','all_available']:
  rr=[r for r in comparisons if r['baseline_days']==n and r['scope']==scope]
  x={'scope':scope,'baseline_days':n,'post_days':3,'sku_count':len(rr),'eligible_sku_count':sum(r['comparison_complete_on_selected_dates'] for r in rr),'constant_pairs':len(cohorts[n]) if scope=='constant_pairs' else None,'all_found_pairs':len(pairs)}
  for m in metrics:
   x['post_'+m]=sum((r['post_total_'+m] for r in rr),D(0))
   x['expected_'+m]=sum((r['expected_post_'+m] for r in rr),D(0))
   x['delta_'+m]=x['post_'+m]-x['expected_'+m]
  x['spend_delta_percent']=ratio(x['delta_spend_rub'],x['expected_spend_rub'],100)
  summary.append(x)
write('revised_ad_summary.csv',summary)
# Metadata and main report: exactly 20 rows, not 36 placements.
items=load('revision_raw/product_info_targets_candidate.json')['body']['items'];mi={str(x['sku']):x for x in items};sales=read('revised_sales_windows.csv');saleidx={(r['scope'],int(r['baseline_days'])):r for r in sales}
main=[]
for s in TARGET:
 it=mi[s];bb=[r for r in bids if r['sku']==s]
 x={'sku':s,'article':it['offer_id'],'name':it['name'],'campaigns_and_current_bids':'; '.join(f"{r['campaign_id']}={r['bid_rub']} RUB ({r['campaign_state']})" for r in bb),'current_bids_source':'current_bids.csv; original snapshot 2026-11-03','original_bid':'не восстановлено','installed_20261030_bid':'не восстановлено','bid_history_source':'манифест и журнал изменения 30.10 не восстановлены; текущая ставка не доказывает прошлую','stock_has_stock':it.get('stocks',{}).get('has_stock'),'stock_by_source':json.dumps(it.get('stocks',{}).get('stocks',[]),ensure_ascii=False),'availability_by_source':json.dumps(it.get('availabilities',[]),ensure_ascii=False),'visibility_details':json.dumps(it.get('visibility_details',{}),ensure_ascii=False),'stock_snapshot_utc':load('revision_raw/product_info_targets_candidate.json')['retrieved_at_utc'],'historical_stock_availability':'не восстановлено','current_family_model_id':it.get('model_info',{}).get('model_id'),'current_family_reported_count':it.get('model_info',{}).get('count'),'family_basis':'current Seller model_id snapshot 2026-11-03; historical membership unavailable','post_period':'2026-10-31..2026-11-02','post_full_days':3,'post_7_days':'ещё не накоплены','post_14_days':'ещё не накоплены','post_28_days':'ещё не накоплены'}
 for n in bases:
  z=per[(s,'all_available',n)]
  for k,v in z.items():
   if k not in ['sku','scope','baseline_days']:x[f'ad_all_base{n}_'+k]=v
  z=per[(s,'constant_pairs',n)]
  for k in ['placement_pairs','expected_post_spend_rub','post_total_spend_rub','delta_spend_rub','spend_delta_percent','expected_post_ad_orders_api','post_total_ad_orders_api']:
   x[f'ad_constant_base{n}_'+k]=z[k]
  for scope,label in [('sku_'+s,'self'),('family_'+s,'family_current')]:
   sr=saleidx[(scope,n)]
   for k,v in sr.items():
    if k not in ['scope','baseline_days','post_days','composition_basis']:x[f'sales_{label}_base{n}_'+k]=v
 main.append(x)
assert len(main)==len({r['sku'] for r in main})==20
write('revised_sku_summary_20.csv',main)
# Calendar and honest quality denominator: any returned row is not complete campaign coverage.
quality=[]
for s in TARGET:
 for date in base28+['2026-10-30']+post:
  got=[r for r in rows if r['sku']==s and r['date_msk']==date]
  quality.append({'sku':s,'date_msk':date,'status':'есть строки' if got else 'нет данных','returned_campaigns':';'.join(sorted(r['campaign_id'] for r in got)),'returned_rows':len(got),'full_historical_campaign_coverage_confirmed':False})
write('revised_sku_day_coverage.csv',quality)
# Plan evidence, including old campaigns selected by actual overlap, not by a new-campaign-only filter.
refresh=load('revision_raw/campaigns_refresh.json');cs=refresh['body']['list'];meta={str(c['id']):c for c in cs};prepids={'49173478':'new','49173499':'bundles','49173502':'old'};prepared={};preparedrows=[]
for cid,stream in prepids.items():
 v=load('revision_raw/products_refresh_'+cid+'.json')
 for p in v['products']:
  s=str(p['sku']);assert s not in prepared;prepared[s]=stream
  preparedrows.append({'sku':s,'stream':stream,'preparation_campaign_id':cid,'title':p.get('title'),'bid_micro_rub_snapshot':p.get('bid'),'classification_basis':'user-confirmed mapping of preparation campaigns to new/bundles/old','source':'revision_raw/products_refresh_'+cid+'.json','immutable_manifest_match_verified':False,'individual_floor_compliance_verified':False})
assert len(prepared)==2953
write('revised_prepared_composition_2953.csv',preparedrows)
referenceplan={'source':'user instruction in current task 2026-11-03','immutable_manifest_sku_count':2963,'immutable_manifest_sha256':'54f332222592045527346f050d331d065c601ec1adec90a19b9b1e9fb95325c2','immutable_original_file_available':False,'prepared_sku_count':2953,'prepared_by_stream':dict(collections.Counter(prepared.values())),'first_wave_authorized':['2026-11-02','2026-11-08'],'novelties_total_approx':1379,'bundles_total':None,'old_total':None,'individual_floor_mandatory':True,'individual_floor_execution_verified':False,'expense_reference_rub':12500,'technical_weekly_budget_rub':5906000,'technical_budget_is_expense_plan':False,'future_weekly_allocation_recovered':False,'unexplained_manifest_to_prepared_difference':10}
save('revised_plan_reference.json',referenceplan)
# Evidence for previously prepared products in any returned CPC report; no wave attribution is inferred.
candidate_ids=set(prepids)|{'49321871'}|{x['campaign_id'] for x in load('revised_existing_campaign_overlaps.json')}
statby=collections.defaultdict(lambda: {'views':D(0),'spend_rub':D(0),'positive_skus':set(),'returned_skus':set()})
for p in sorted((O/'raw').glob('sku_report*_result.json')):
 for cid,block in load(str(p.relative_to(O)))['body'].items():
  if not isinstance(block,dict):continue
  for r in block.get('report',{}).get('rows',[]) or []:
   s=str(r.get('sku'));date=day(r['date'])
   if s not in prepared:continue
   key=(cid,date,prepared[s]);z=statby[key];z['returned_skus'].add(s)
   views=num(r.get('views',0));money=num(r.get('moneySpent','0'));z['views']+=views;z['spend_rub']+=money
   if views>0 or money>0:z['positive_skus'].add(s)
context=[]
for (cid,date,stream),z in sorted(statby.items()):context.append({'campaign_id':cid,'date_msk':date,'stream_by_prepared_membership':stream,'returned_prepared_skus':len(z['returned_skus']),'positive_prepared_skus':len(z['positive_skus']),'views':z['views'],'spend_rub':z['spend_rub'],'wave_membership':'не подтверждена; контекст, не результат первой волны'})
if context:write('revised_prepared_ad_context.csv',context)
campdaily=read('campaign_daily.csv');compositions=[];evidence=[]
for cid in sorted(candidate_ids):
 p=R/f'products_refresh_{cid}.json';v=json.loads(p.read_text());ps=v['products'];ss={str(x['sku']) for x in ps};c=meta[cid]
 for product in ps:compositions.append({'campaign_id':cid,'sku':str(product['sku']),'title':product.get('title'),'stream':prepared.get(str(product['sku']),'unknown'),'classification_source':'prepared membership with user-confirmed stream rule' if str(product['sku']) in prepared else 'неизвестно; название кампании не используется как классификатор','bid_micro_rub':product.get('bid'),'snapshot_utc':v['retrieved_at_utc'],'source':str(p.relative_to(O))})
 dr=[r for r in campdaily if r['campaign_id']==cid];activity=[r for r in dr if any(num(r[k]) and num(r[k])>0 for k in ['views','clicks','spend_rub'])]
 preactivity=[r for r in activity if r['date_msk']<'2026-11-02'];waveactivity=[r for r in activity if '2026-11-02'<=r['date_msk']<='2026-11-03']
 context_before=[z for z in context if z['campaign_id']==cid and z['date_msk']<'2026-11-02' and z['positive_prepared_skus']]
 context_after=[z for z in context if z['campaign_id']==cid and z['date_msk']=='2026-11-02' and z['positive_prepared_skus']]
 role='preparation' if cid in prepids else ('automatic_new_candidate' if cid=='49321871' else 'older_campaign_with_prepared_overlap')
 evidence.append({'campaign_id':cid,'role':role,'title':c['title'],'created_confirmed':True,'created_at_utc':c['createdAt'],'from_date':c.get('fromDate'),'to_date':c.get('toDate'),'state_now':c['state'],'is_autocreated':c.get('isAutocreated'),'payment_type':c.get('PaymentType'),'product_campaign_mode':c.get('productCampaignMode'),'strategy':c.get('productAutopilotStrategy'),'technical_weekly_budget_rub':num(c.get('weeklyBudget','0'))/D(1000000),'activation_ever_proven_by_activity':bool(activity),'activation_timestamp':'не восстановлено','activity_before_wave':bool(preactivity),'activity_in_wave_dates_observed':bool(waveactivity),'first_observed_positive_date':min((r['date_msk'] for r in activity),default=None),'current_sku_count':len(ss),'overlap_with_prepared_2953':len(ss&set(prepared)),'current_streams':json.dumps(dict(collections.Counter(prepared.get(s,'unknown') for s in ss)),ensure_ascii=False),'prepared_sku_positive_before_wave':bool(context_before),'prepared_sku_positive_on_nov2':bool(context_after),'wave_membership':'не подтверждена','membership_evidence_limit':'2953-set overlap is supporting context only; history and final activation manifest absent' if ss&set(prepared) else 'outside prepared 2953 SKU; no source connects campaign to approved wave','composition_source':str(p.relative_to(O))})
write('revised_wave_candidate_evidence.csv',evidence);write('revised_wave_candidate_composition.csv',compositions)
# Plan counts distinguish preparation, observed work, and unverified launch membership.
plan=[]
for stream,allcount,note in [('new','около 1379','ввод частями'),('bundles','не восстановлено числом; весь согласованный объём','продающиеся и непродающиеся, ввод частями'),('old','не восстановлено','старые позиции вне рекламы, ввод частями')]:
 n=sum(v==stream for v in prepared.values())
 plan.append({'stream':stream,'total_planned':allcount,'plan_rule':note,'first_week_authorized':'2026-11-02..2026-11-08','first_week_prepared_count':n,'original_first_week_manifest_count_by_stream':'не восстановлено (2963 всего до проверок)','factually_confirmed':'подготовлено '+str(n)+' SKU в кампании '+next(cid for cid,t in prepids.items() if t==stream)+'; принадлежность работающих размещений волне не подтверждена','confirmed_launched_wave_skus':'не установлено','remaining_to_confirmed_launch':'не установлено; подготовка не равна запуску','future_waves':'числа и даты не восстановлены','source':'user plan + revised_prepared_composition_2953.csv + revised_wave_candidate_evidence.csv'})
write('revised_three_stream_plan.csv',plan)
# Available cabinet spend by actual payment model, never assumed CPO percent.
cab=[]
salesdaily={(r['scope'],r['date_msk']):r for r in read('revised_sales_daily.csv')}
for date in base28+post:
 out={'date_msk':date,**{k:salesdaily[('cabinet',date)][k] for k in ['orders','postings','units','ordered_value_rub']}}
 for pay in ['CPC','CPO']:
  rr=[r for r in campdaily if r['date_msk']==date and meta[r['campaign_id']].get('PaymentType')==pay]
  out[pay.lower()+'_reported_spend_rub']=sum((num(r['spend_rub']) for r in rr),D(0)) if rr else None
  out[pay.lower()+'_returned_campaign_rows']=len(rr)
 out['spend_scope']='sum of returned campaign/day moneySpent; missing campaign rows not zero-filled; not split into cash/bonus'
 cab.append(out)
write('revised_cabinet_daily.csv',cab)
cabcomparisons=[]
for n,base in bases.items():
 for m in ['cpc_reported_spend_rub','cpo_reported_spend_rub']:
  ix={r['date_msk']:r for r in cab}
  expected=sum((sum((ix[b][m] for b in reference[n][d]),D(0))/D(n//7) for d in post),D(0));actual=sum((ix[d][m] for d in post),D(0))
  cabcomparisons.append({'baseline_days':n,'metric':m,'post_days':3,'post_total':actual,'expected_post_same_weekdays':expected,'delta':actual-expected,'delta_percent':ratio(actual-expected,expected,100)})
write('revised_cabinet_spend_comparison.csv',cabcomparisons)
qualitysummary={'requested_skus':20,'main_unique_sku_rows':20,'original_detail_rows':len(rows),'reported_sku_days':len(anyday),'requested_sku_days':640,'meaning_of_638_640':'at least one explicit row in available campaigns per SKU/day; not complete historical placement coverage','missing_sku_days':[{'sku':s,'date':d} for s in TARGET for d in base28+['2026-10-30']+post if (s,d) not in anyday],'gap_requery':'both specific missing days re-requested in original known current campaigns; still no target rows; not zero-filled','cpc_campaigns_reported':31,'archived_composition_http400':9,'archived_limitation':'all returned archived campaigns were included in statistics requests; absent historical membership prevents proof of complete SKU campaign universe','current_pairs':len(pairs),'constant_pairs_by_baseline':{str(n):len(v) for n,v in cohorts.items()},'comparison_post_days':3,'base14_start':base14[0] if (base14:=bases[14]) else None,'base28_start':base28[0],'post_end_yesterday_msk':'2026-11-02','post7_14_28_status':'ещё не накоплены','immutable_manifest_verified':False,'candidate_current_sku_not_in_prepared_set':'6201364326','cause_effect_claimed':False,'late_attribution':'post-period orders may mature; cancelled status and stock are current snapshots'}
save('revised_quality_summary.json',qualitysummary)
save('revised_comparison_design.json',{'base14':bases[14],'base28':bases[28],'post_full_days':post,'excluded_mixed_day':'2026-10-30','references_by_baseline':reference,'formula':'Expected_post(metric)=Σ_post_day mean(metric on same weekday dates within baseline). Expected_daily=Expected_post/3; Δ=actual post−expected post. Raw baseline daily means divide only observed SKU-days and disclose N; full-window means unavailable when a day is missing. CPC=Σspend/Σclicks; DRR=100×Σspend/Σad_revenue, never average daily ratios.','two_scopes':['constant_pairs: fixed eligible SKU×campaign subset for each baseline','all_available: each unique SKU×campaign×day row exactly once, then aggregate to fixed20 SKU/day'],'family_dedup':'current model_id for all noncancelled sold SKU, union by posting product line; no historical composition claim'})
for z in summary:print(z['scope'],'base',z['baseline_days'],'pairs',z['constant_pairs'],'spend',z['post_spend_rub'],'expected',z['expected_spend_rub'],'delta%',z['spend_delta_percent'],'orders',z['post_ad_orders_api'],z['expected_ad_orders_api'])
print('Prepared contexts on Nov 2 with positive traffic:',[(r['campaign_id'],r['positive_prepared_skus']) for r in context if r['date_msk']=='2026-11-02' and r['positive_prepared_skus']])
print('Main output:',len(main),'unique SKU; candidate evidence:',len(evidence),'campaigns')

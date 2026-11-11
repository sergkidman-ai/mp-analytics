# поток: mkt
import pathlib,json,csv,collections,datetime as dt,decimal,hashlib
from zoneinfo import ZoneInfo
O=pathlib.Path(__file__).parent;ROOT=O.parent;D=decimal.Decimal;TZ=ZoneInfo('Europe/Moscow')
def load(n):return json.loads((O/n).read_text())
def save(n,x):(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str)+'\n')
def write(n,rr):
 with (O/n).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rr[0]));w.writeheader();w.writerows(rr)
rows=load('eligibility_final.json');good=[r for r in rows if r['passed_all_checks']];bad=[r for r in rows if not r['passed_all_checks']]
prepared=[];excluded=[]
famaudit={r['model_id']:r for r in load('family_audit.json')}
for r in rows:
 x={k:r.get(k) for k in ['sku','stream','product_id','offer_id','name','model_id','price_rub','stock_has_stock','metadata_source']};x['reasons']=';'.join(r['reasons'])
 if r['passed_all_checks']:
  x.update(r['floor']);prepared.append(x)
 else:
  x['family_known_products']=famaudit.get(r['model_id'],{}).get('known_product_ids');x['family_member_failures']=json.dumps(famaudit.get(r['model_id'],{}).get('failed_member_details',{}),ensure_ascii=False);excluded.append(x)
write('ready_skus.csv',prepared);write('excluded_skus.csv',excluded)
# Deterministic <=500-SKU campaign packs; keep each stream's family block together.
campaigns=[]
for stream in ['new','bundles','old']:
 groups=collections.defaultdict(list)
 for r in good:
  if r['stream']==stream:groups[r['model_id']].append(r)
 packs=[]
 for model,rr in sorted(groups.items(),key=lambda t:(-len(t[1]),t[0])):
  assert len(rr)<=500
  dest=next((x for x in packs if len(x)+len(rr)<=500),None)
  if dest is None:dest=[];packs.append(dest)
  dest.extend(sorted(rr,key=lambda r:r['sku']))
 for i,rr in enumerate(packs,1):
  key=f'{stream}_{i:02}';title=f'oz_acc1_wave1_recovered_20261103_{key}_fde1af96'
  campaigns.append({'key':key,'stream':stream,'title':title,'sku_count':len(rr),'weekly_budget_rub':len(rr)*2000,'weekly_budget_micro_rub':str(len(rr)*2000*1000000),'family_ids':sorted({r['model_id'] for r in rr}),'sku_bids':[{'sku':r['sku'],'bid_rub':r['floor']['bid_rub'],'bid_micro_rub':r['floor']['bid_micro_rub'],'floor_checked_at_utc':r['floor']['checked_at_utc'],'floor_source':r['floor']['source'],'model_id':r['model_id']} for r in rr]})
now=dt.datetime.now(TZ)
# Fixed proposal, not a scheduled launch. Do not silently move this start on an apply step.
threshold=now+dt.timedelta(minutes=20);start=threshold.replace(minute=(threshold.minute//15)*15,second=0,microsecond=0)+dt.timedelta(minutes=15)
stop=dt.datetime(2026,11,8,23,59,tzinfo=TZ);boundary=dt.datetime(2026,11,9,0,0,tzinfo=TZ)
assert start.date()==dt.date(2026,11,3) and start<stop
meta=load('raw/campaigns.json')['body']['list']
for c in campaigns:
 c['create_payload_preview']={'title':c['title'],'fromDate':start.date().isoformat(),'toDate':'2026-11-08','placement':'PLACEMENT_SEARCH_AND_CATEGORY','productCampaignMode':'PRODUCT_CAMPAIGN_MODE_AUTO','productAutopilotStrategy':'TARGET_BIDS','PaymentType':'CPC','expenseStrategy':'DAILY_BUDGET','budgetType':'PRODUCT_CAMPAIGN_BUDGET_TYPE_WEEKLY','weeklyBudget':c['weekly_budget_micro_rub'],'startWeekDay':'TUESDAY','endWeekDay':'MONDAY','autoIncreasePercent':0}
source=ROOT/'revised_prepared_composition_2953.csv'
byfamily=collections.defaultdict(set)
for r in good:byfamily[r['model_id']].add(r['stream'])
manifest={'schema_version':1,'account':'oz_acc1','status':'PREPARED_ONLY_NOT_APPLIED','created_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'source_path':str(source.resolve()),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'source_kind':'recovered preparation campaign composition on 2026-11-03','lost_original_2963_manifest_sha256':'54f332222592045527346f050d331d065c601ec1adec90a19b9b1e9fb95325c2','lost_original_match_verified':False,'source_sku_count':2953,'ready_sku_count':len(good),'excluded_sku_count':len(bad),'paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU','placement':'PLACEMENT_SEARCH_AND_CATEGORY','individual_floor_endpoint':'POST /api/client/min/sku','individual_floor_api_unit':'RUB per click','product_bid_payload_unit':'micro-rubles, exactly RUB × 1000000','floor_records_count':len(load('individual_floors.json')),'proposed_start_at_msk':start.isoformat(),'scheduled_stop_at_msk':stop.isoformat(),'agreed_end_boundary_exclusive_msk':boundary.isoformat(),'scheduled_stop_at_utc':stop.astimezone(dt.timezone.utc).isoformat(),'duration_to_scheduled_stop_seconds':int((stop-start).total_seconds()),'duration_to_agreed_boundary_seconds':int((boundary-start).total_seconds()),'last_minute_reserved_for_stop_verification_seconds':60,'end_date_automatically_extended':False,'native_weekly_minimum_rub_per_sku':2000,'weekly_budget_rub_total':len(good)*2000,'weekly_budget_is_actual_expense_forecast':False,'actual_expense_reference_rub':12500,'actual_expense_reference_is_hard_cap':False,'campaigns':campaigns,'campaign_chunk_size_policy':'operational ceiling <=500, not asserted as current vendor total limit; same stream family block not split','cross_stream_current_families':{k:sorted(v) for k,v in byfamily.items() if len(v)>1},'family_exclusion_rule':'fail any member or unknown full membership => exclude every recovered member of model_id; all active CPC family overlaps excluded','forbidden_existing_campaign_ids':[str(c['id']) for c in meta],'forbidden_preparation_campaign_ids':['49173478','49173499','49173502'],'mutations_applied':False,'stop_status':'local systemd marker tested; real timer/unit NOT installed, campaign bindings empty','launch_conditions':['explicit approval of this exact manifest hash, budget and shortened period','refresh gates and floors if launch is delayed or metadata/price changes','new campaign IDs created in non-running state and verified; do not activate preparation IDs','bind only new IDs to stop operator and install/enable persistent timer before activation','verify stop API authorization/end-to-end on approved created campaign before live activation; no stop write was tested in this read-only task','coordinated activation of all streams; partial activation requires stopping approved new wave IDs; shared family outcome uses deduplicated union','if proposed start missed, rebuild and explicitly approve new start while keeping 8-Nov boundary unchanged']}
save('launch_manifest.json',manifest);digest=hashlib.sha256((O/'launch_manifest.json').read_bytes()).hexdigest();(O/'launch_manifest.sha256').write_text(digest+'  launch_manifest.json\n')
save('created_campaign_bindings.json',{'account':'oz_acc1','manifest_sha256':digest,'campaigns':[],'state':'UNBOUND_NOT_CREATED'})
summary=[]
for stream in ['new','bundles','old']:
 rr=[r for r in rows if r['stream']==stream];ok=[r for r in rr if r['passed_all_checks']]
 summary.append({'stream':stream,'recovered':len(rr),'passed':len(ok),'excluded':len(rr)-len(ok),'individual_floor_distribution_rub':json.dumps(dict(collections.Counter(r['floor']['bid_rub'] for r in ok)),sort_keys=True),'native_minimum_weekly_budget_rub':len(ok)*2000,'planned_campaign_keys':';'.join(c['key'] for c in campaigns if c['stream']==stream)})
write('launch_summary.csv',summary)
write('campaign_plan.csv',[{k:c[k] for k in ['key','stream','title','sku_count','weekly_budget_rub','weekly_budget_micro_rub']} for c in campaigns])
stoputc=stop.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
cmd=f'/opt/mp-analytics/venv/bin/python {O.resolve()}/deadline_stop.py --manifest {O.resolve()}/launch_manifest.json --manifest-sha256 {digest} --bindings {O.resolve()}/created_campaign_bindings.json --execute'
(O/'mp-ozon-wave1-deadline.service').write_text('[Unit]\nDescription=Stop only approved new oz_acc1 wave1 campaign IDs\nAfter=network-online.target\nWants=network-online.target\nStartLimitIntervalSec=0\n\n[Service]\nType=oneshot\nWorkingDirectory=/opt/mp-analytics\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nExecStart='+cmd+'\nRestart=on-failure\nRestartSec=10s\n')
(O/'mp-ozon-wave1-deadline.timer').write_text('[Unit]\nDescription=Deadline for approved oz_acc1 wave1 only\n\n[Timer]\nOnCalendar='+stoputc+'\nAccuracySec=1s\nRandomizedDelaySec=0\nPersistent=true\nUnit=mp-ozon-wave1-deadline.service\n\n[Install]\nWantedBy=timers.target\n')
(O/'deadline_backup.cron.preview').write_text('# Preview only; NOT installed. Executes no marketplace writes before scheduled stop.\n* * * * * root '+cmd+' >> '+str(O.resolve()/'deadline_stop.log')+' 2>&1\n')
print('Manifest SHA256:',digest)
print('Summary:',summary)
print('Campaigns:',[(c['key'],c['sku_count'],c['weekly_budget_rub']) for c in campaigns])
print('Proposed start',start.isoformat(),'scheduled stop',stop.isoformat(),'agreed boundary',boundary.isoformat())
print('Shared across streams families',len(manifest['cross_stream_current_families']))

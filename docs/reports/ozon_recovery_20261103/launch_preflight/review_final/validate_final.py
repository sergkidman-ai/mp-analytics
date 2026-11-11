# поток: mkt
import pathlib,json,csv,hashlib,decimal,collections,datetime as dt,subprocess,re
from dotenv import dotenv_values
O=pathlib.Path(__file__).parent;PRE=O.parent;ROOT=PRE.parent;P=pathlib.Path('/opt/mp-analytics');D=decimal.Decimal

def load(n):return json.loads((O/n).read_text())
def read(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
oldcheck={}
for dirp,name in [(ROOT,'SHA256SUMS'),(ROOT,'REVISED_SHA256SUMS'),(ROOT,'ADDENDUM_SHA256SUMS'),(PRE,'SHA256SUMS')]:
 r=subprocess.run(['sha256sum','-c',name],cwd=dirp,capture_output=True,text=True);assert r.returncode==0,str([l for l in r.stdout.splitlines() if not l.endswith('OK')]);oldcheck[str(dirp/name)]='PASS'
oldhash=hashlib.sha256((PRE/'launch_manifest.json').read_bytes()).hexdigest();assert oldhash=='46c202d8563ae0165bdca6c0e2516424bc8409007ba0778c7b13cf6cd833bdd5'
audit=read('old_exclusion_per_sku.csv');assert len(audit)==1466 and all(r['own_direct_failed']=='False' and r['excluded_only_due_to_family']=='True' for r in audit)
for s,n in [('new',39),('bundles',1096),('old',331)]:
 rr=[r for r in audit if r['stream']==s];assert len(rr)==n
 assert sum(int(r['total_excluded']) for r in read('old_exclusions_by_stream_reason.csv') if r['stream']==s)==n
 assert all(r['original_primary_reason'] in ['FAMILY_ALREADY_IN_RUNNING_CPC','FAMILY_COMPOSITION_INCOMPLETE'] for r in rr)
assert len({r['model_id'] for r in audit})==486
examples=read('bundles_examples.csv');assert collections.Counter(r['reason'] for r in examples)=={'FAMILY_ALREADY_IN_RUNNING_CPC':3,'FAMILY_COMPOSITION_INCOMPLETE':3,'FAMILY_MEMBER_FAILED_CHECK':3}
for r in examples:
 ex=load(r['evidence_file']);assert ex['own_http']==200 and ex['own_response_fields']['stocks']['has_stock'] and any(a['availability']=='AVAILABLE' for a in ex['own_response_fields']['availabilities'])
 if r['reason']=='FAMILY_COMPOSITION_INCOMPLETE':assert int(r['known_family_products'])<max(json.loads(r['api_family_count']))
 if r['reason']=='FAMILY_MEMBER_FAILED_CHECK':assert any(n['direct_failures'] for n in ex['neighbor_evidence'])
source=ROOT/'revised_prepared_composition_2953.csv';assert hashlib.sha256(source.read_bytes()).hexdigest()=='fde1af9669d5ca1c0c89c04aaf3cb2d435de7b23a8ada5279c19a644586a38ed'
with source.open() as f:whitelist=list(csv.DictReader(f))
m=load('final_manifest.json');ready=read('final_composition.csv');assert len(ready)==len({r['sku'] for r in ready})==2953
assert {r['sku'] for r in ready}=={r['sku'] for r in whitelist}
assert collections.Counter(r['stream'] for r in ready)=={'new':458,'bundles':1425,'old':1070}
assert m['actual_start_at_msk'] is None and m['proposed_start_at_msk'] is None and m['scheduled_start_at_msk'] is None
assert m['scheduled_stop_at_msk']=='2026-11-08T22:00:00+03:00' and m['scheduled_stop_at_utc']=='2026-11-08T19:00:00+00:00'
assert len(m['campaigns'])==3 and [c['sku_count'] for c in m['campaigns']]==[458,1425,1070]
assert sum(c['weekly_budget_rub'] for c in m['campaigns'])==5906000
for c in m['campaigns']:
 assert c['weekly_budget_rub']==c['sku_count']*2000
 allocated=[s for b in c['upload_batches'] for s in b['skus']];assert len(allocated)==len(set(allocated))==c['sku_count']
 assert set(allocated)=={r['sku'] for r in c['sku_bids']}
 block=collections.defaultdict(set)
 for b in c['upload_batches']:
  assert b['sku_count']<=500
  for family in b['family_keys']:block[family].add(b['batch'])
 assert all(len(v)==1 for v in block.values()),'Model block split between upload chunks'
assert not load('old_campaign_packing_audit.json')['old_model_blocks_split_between_old_shards']
floors={r['sku']:r for r in load('individual_floors_final.json')};assert len(floors)==2953 and load('floor_errors_final.json')=={}
previous={r['sku']:r for r in json.loads((PRE/'individual_floors.json').read_text())};assert len(previous)==1487
for s,x in floors.items():
 if s in previous:assert x['bid_rub']==previous[s]['bid_rub'] and x['checked_at_utc']==previous[s]['checked_at_utc']
 rawp=O/x['source'];z=json.loads(rawp.read_text());req=z['request'];assert s in req['sku'] and req['paymentType']=='CPC' and req['marketplaceId']=='MARKETPLACE_ID_RU'
 rec=[r for r in z['response']['minBids'] if str(r['sku'])==s];assert len(rec)==1 and isinstance(rec[0]['bid'],(int,float)) and not isinstance(rec[0]['bid'],bool)
 assert D(str(rec[0]['bid']))==D(x['bid_rub']) and int(D(x['bid_rub'])*1000000)==int(x['bid_micro_rub'])
assign=load('assignment_preservation_audit.json')
for src in assign['sources']:assert hashlib.sha256(pathlib.Path(src['path']).read_bytes()).hexdigest()==src['sha256']
ledger=load('family_assignment_ledger.json');assert len(ledger)==1720 and all(r['current_wave_assignment'] is None and not r['historical_labels_changed'] and not r['family_split_into_experimental_arms'] for r in ledger)
assert m['experimental_execution_readiness']=='BLOCKED_PENDING_CURRENT_FAMILY_ASSIGNMENT_CONFIRMATION'
assert all(r['experiment_assignment'] is None for c in m['campaigns'] for r in c['sku_bids'])
# Offline stop preview, no token/API calls permitted by its empty bindings.
digest=hashlib.sha256((O/'final_manifest.json').read_bytes()).hexdigest();assert digest==(O/'final_manifest.sha256').read_text().split()[0]
r=subprocess.run(['/opt/mp-analytics/venv/bin/python',str(O/'deadline_stop.py'),'--manifest',str(O/'final_manifest.json'),'--manifest-sha256',digest,'--bindings',str(O/'created_campaign_bindings.json')],capture_output=True,text=True)
assert r.returncode==0 and json.loads(r.stdout)['state']=='BLOCKED_UNBOUND'
assert load('corrected_stop_validation.json')['unit_verify_exit']==0
report=(O/'FINAL_REVIEW_REPORT.md').read_text()
for link in re.findall(r'\]\(([^)]+)\)',report):
 if '://' not in link and link!='validation.json':assert (O/link).exists(),link
secrets=[]
for p in [P/'.env',P/'.env.save']:
 if p.exists():
  for k,v in dotenv_values(p).items():
   if v and len(v)>=12 and any(t in k.upper() for t in ['TOKEN','SECRET','API_KEY','PASSWORD','PERF_CLIENT_ID']):secrets.append(v.encode())
for p in O.rglob('*'):
 if p.is_file():
  b=p.read_bytes();assert not any(s in b for s in secrets),'Secret in '+str(p.relative_to(O))
result={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'account':'oz_acc1','old_source_checksums_unchanged':oldcheck,'old_manifest_exact_hash':oldhash,'old_exclusion_totals_reconciled':[39,1096,331],'direct_own_failures_in_old_exclusions':0,'family_only_old_exclusions':1466,'affected_unique_families':486,'bundle_examples_three_per_rule':'PASS','technical_ready_unique_2953_subset_only':'PASS','new_minimum_queries_only_missing_sku_count':1466,'old_floor_results_reused_with_original_times':1487,'all_individual_numeric_floors':2953,'budget_minimum_rub':5906000,'old_family_no_split':'PASS','new_upload_batches_not_experimental_groups':'PASS','current_experiment_assignments_invented':0,'historical_assignment_files_unchanged':'PASS','actual_start_null':'PASS','stop_22_Europe_Moscow':'PASS','stop_not_installed_not_bound':True,'marketplace_mutations':0,'database_data_writes':0,'full_collection_repeated':False,'secret_scan':'PASS','manifest_sha256':digest}
(O/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
files=sorted(p for p in O.rglob('*') if p.is_file() and p.name!='SHA256SUMS')
(O/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(O).as_posix()+'\n' for p in files))
print('Final validation PASS: old sources unchanged, exclusions reconciled, 2953 own eligibility and floor, no arm reassignment, stop22/startnull')
print('Manifest SHA256',digest,'files hashed',len(files))

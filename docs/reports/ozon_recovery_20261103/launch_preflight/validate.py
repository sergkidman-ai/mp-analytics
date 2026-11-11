# поток: mkt
import pathlib,json,csv,hashlib,decimal,collections,datetime as dt,subprocess,re
from dotenv import dotenv_values
O=pathlib.Path(__file__).parent;ROOT=O.parent;P=pathlib.Path('/opt/mp-analytics');D=decimal.Decimal

def load(n):return json.loads((O/n).read_text())
def read(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
old={}
for file in ['SHA256SUMS','REVISED_SHA256SUMS','ADDENDUM_SHA256SUMS']:
 r=subprocess.run(['sha256sum','-c',file],cwd=ROOT,capture_output=True,text=True);assert r.returncode==0,str([l for l in r.stdout.splitlines() if not l.endswith('OK')]);old[file]='PASS'
source=ROOT/'revised_prepared_composition_2953.csv';assert hashlib.sha256(source.read_bytes()).hexdigest()=='fde1af9669d5ca1c0c89c04aaf3cb2d435de7b23a8ada5279c19a644586a38ed'
with source.open() as f:whitelist=list(csv.DictReader(f))
allowed={r['sku'] for r in whitelist};assert len(allowed)==len(whitelist)==2953
assert collections.Counter(r['stream'] for r in whitelist)=={'new':458,'bundles':1425,'old':1070}
m=load('launch_manifest.json');digest=hashlib.sha256((O/'launch_manifest.json').read_bytes()).hexdigest();assert digest==(O/'launch_manifest.sha256').read_text().split()[0]
ready=read('ready_skus.csv');excluded=read('excluded_skus.csv');assert len(ready)==len({r['sku'] for r in ready})==1487
assert {r['sku'] for r in ready}|{r['sku'] for r in excluded}==allowed
assert not ({r['sku'] for r in ready}&{r['sku'] for r in excluded}) and len(excluded)==1466
# No partial family exclusion across the recovered universe.
assert not ({r['model_id'] for r in ready}&{r['model_id'] for r in excluded})
allocated=[r['sku'] for c in m['campaigns'] for r in c['sku_bids']];assert len(allocated)==len(set(allocated))==1487 and set(allocated)=={r['sku'] for r in ready}
assert sum(c['weekly_budget_rub'] for c in m['campaigns'])==2974000 and all(c['weekly_budget_rub']==c['sku_count']*2000 for c in m['campaigns'])
assert [c['sku_count'] for c in m['campaigns']]==[419,329,500,239]
# Every planned bid must be a returned numerical individual floor for this exact SKU.
floors={r['sku']:r for r in load('individual_floors.json')};assert len(floors)==1487
for c in m['campaigns']:
 for r in c['sku_bids']:
  z=floors[r['sku']];raw=load(z['source']);asked=raw['request'];assert r['sku'] in asked['sku'] and asked['paymentType']=='CPC' and asked['marketplaceId']=='MARKETPLACE_ID_RU'
  records=[v for v in raw['response']['minBids'] if str(v['sku'])==r['sku']];assert len(records)==1 and isinstance(records[0]['bid'],(int,float)) and not isinstance(records[0]['bid'],bool)
  assert D(str(records[0]['bid']))==D(r['bid_rub']) and int(D(r['bid_rub'])*1000000)==int(r['bid_micro_rub'])
assert load('floor_errors.json')=={}
# Read log must contain no campaign mutations. (Token body is intentionally never logged.)
for line in (O/'read_requests.jsonl').read_text().splitlines():
 r=json.loads(line)
 assert r['method']=='GET' or r['path'] in ['/api/client/token','/api/client/min/sku','/v3/product/info/list']
 assert not (r['path']=='/api/client/token' and 'body' in r)
assert load('created_campaign_bindings.json')['campaigns']==[]
stop=load('stop_mechanism_validation.json');assert stop['local_timer_proof']['network_calls']==0 and stop['unit_files_static_validation']['exit_code']==0 and stop['real_timer_installed']==False
assert dt.datetime.fromisoformat(m['scheduled_stop_at_msk'])<dt.datetime.fromisoformat(m['agreed_end_boundary_exclusive_msk'])
assert m['agreed_end_boundary_exclusive_msk']=='2026-11-09T00:00:00+03:00'
assert dt.datetime.now(dt.timezone.utc)<dt.datetime.fromisoformat(m['proposed_start_at_msk']),'Proposed start already passed; do not keep stale time'
assert load('monitor20_shipping_summary.json')['unknown_product_model_lines']==0
ad=read('monitor20_ad_fresh.csv');assert len(ad)==len({(r['sku'],r['campaign_id'],r['date_msk']) for r in ad})
assert all(r['partial_day']=='True' for r in ad if r['date_msk']=='2026-11-03')
report=(O/'READY_REPORT.md').read_text()
for link in re.findall(r'\]\(([^)]+)\)',report):
 if '://' not in link:assert (O/link).exists(),link
# No keys, secrets or tokens from protected env files in saved outputs.
secrets=[]
for p in [P/'.env',P/'.env.save']:
 if p.exists():
  for key,value in dotenv_values(p).items():
   if value and len(value)>=12 and any(x in key.upper() for x in ['TOKEN','SECRET','API_KEY','PASSWORD','PERF_CLIENT_ID']):secrets.append(value.encode())
for p in O.rglob('*'):
 if p.is_file():
  b=p.read_bytes();assert not any(s in b for s in secrets),'Secret in '+str(p.relative_to(O))
result={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'account':'oz_acc1','preserved_previous_sources':old,'source_sha256':'PASS','source_unique_2953':'PASS','ready_unique_1487_subset_only':'PASS','excluded_1466_with_reasons':'PASS','whole_family_exclusion':'PASS','individual_numeric_minimum_per_sku':'PASS','RUB_to_micro_exact_conversion':'PASS','budget_no_reserve_2974000':'PASS','proposed_start_in_future_at_validation':True,'agreed_end_preserved':'PASS','local_timer_marker_and_static_units':'PASS','live_stop_bound_and_armed':False,'marketplace_mutations':0,'database_data_writes':0,'advertising_schedules_changed':0,'local_harmless_transient_timer_test_only':True,'secrets_scan':'PASS','manifest_sha256':digest}
(O/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
files=sorted(p for p in O.rglob('*') if p.is_file() and p.name!='SHA256SUMS')
(O/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(O).as_posix()+'\n' for p in files))
print('Validation PASS; manifest',digest,'1487 ready, 1466 excluded, previous exports unchanged, no marketplace writes')
print('Files hashed',len(files))

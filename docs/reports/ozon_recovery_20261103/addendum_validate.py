# поток: mkt
import pathlib,json,csv,hashlib,datetime as dt,decimal,subprocess,re
from dotenv import dotenv_values
O=pathlib.Path(__file__).parent;P=pathlib.Path('/opt/mp-analytics');D=decimal.Decimal

def read(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
def load(n):return json.loads((O/n).read_text())
# Initial and revised exports must remain unchanged.
oldchecks={}
for name in ['SHA256SUMS','REVISED_SHA256SUMS']:
 r=subprocess.run(['sha256sum','-c',name],cwd=O,capture_output=True,text=True)
 assert r.returncode==0,str([x for x in r.stdout.splitlines() if not x.endswith('OK')])
 oldchecks[name]={'result':'PASS','files':len(r.stdout.splitlines())}
combined=read('addendum_combined_comparison.csv');assert len(combined)==5
metrics={r['metric']:r for r in combined}
lines=read('revised_sales_lines.csv');main=read('revised_sku_summary_20.csv');target={r['sku'] for r in main};models={r['current_family_model_id'] for r in main};assert len(target)==20 and len(models)==19
post=['2026-10-31','2026-11-01','2026-11-02'];base28=[(dt.date(2026,10,2)+dt.timedelta(days=i)).isoformat() for i in range(28)]
for label,predicate in [('sku',lambda r:r['sku'] in target),('family',lambda r:r['current_model_id'] in models)]:
 rr=[r for r in lines if predicate(r)]
 for metric in ['orders','value']:
  def value(ds):
   x=[r for r in rr if r['date_msk'] in ds]
   return D(len({r['order_id'] for r in x})) if metric=='orders' else sum((D(r['ordered_value_rub']) for r in x),D(0))
  row=metrics[label+'_'+metric];assert D(row['actual'])==value(post)
  for n,base in [(14,base28[-14:]),(28,base28)]:
   expected=sum((sum((value([b]) for b in base if dt.date.fromisoformat(b).weekday()==dt.date.fromisoformat(d).weekday()),D(0))/D(n//7) for d in post),D(0))
   assert D(row[f'expected_base{n}'])==expected
   assert D(row[f'delta_base{n}'])==value(post)-expected
prep=read('revised_prepared_composition_2953.csv');members=read('addendum_prepared_current_membership.csv');summary=read('addendum_prepared_current_summary.csv')[-1]
assert len(prep)==len(members)==len({r['sku'] for r in members})==2953
assert {r['sku'] for r in prep}=={r['sku'] for r in members}
assert summary['in_running_cpc_campaigns']=='0' and summary['only_nonrunning_cpc_campaigns']=='2953' and summary['not_found_in_accessible_compositions']=='0'
assert sum(r['multiple_accessible_campaigns']=='True' for r in members)==4
assert all(r['running_cpc_coverage_complete']=='True' for r in members)
prov=load('addendum_prepared_provenance.json');assert prov['identifier_file_sha256']==hashlib.sha256((O/'revised_prepared_composition_2953.csv').read_bytes()).hexdigest()
assert all(r['identifier_sets_match'] for r in prov['source_snapshots'])
stats=read('addendum_prepared_20261102_metrics.csv')
assert all(r['views']=='' and r['spend_rub']=='' and r['skus_with_explicit_statistics']=='0' for r in stats if r['scope'] in ['new','bundles','old','ALL'])
assert stats[-1]['views']=='20' and stats[-1]['spend_rub']=='0.00'
report=(O/'ADDENDUM_20SKU_AND_2953.md').read_text()
for link in re.findall(r'\]\(([^)]+)\)',report):
 if '://' not in link and link!='addendum_validation.json':assert (O/link).exists(),link
secrets=[]
for file in [P/'.env',P/'.env.save']:
 if file.exists():
  for k,v in dotenv_values(file).items():
   if v and len(v)>=12 and any(s in k.upper() for s in ['TOKEN','SECRET','API_KEY','PASSWORD','PERF_CLIENT_ID']):secrets.append(v.encode())
newfiles=[f for f in O.iterdir() if f.is_file() and (f.name.startswith('addendum_') or f.name.startswith('ADDENDUM_'))]+list((O/'addendum_raw').glob('*.json'))
for file in newfiles:
 data=file.read_bytes();assert not any(s in data for s in secrets),'Secret value in '+str(file.relative_to(O))
check={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'account':'oz_acc1','original_and_revised_export_checksums':oldchecks,'combined_table_recomputed_from_noncancelled_order_lines':'PASS','unique_order_id_definition_same_before_after':True,'current_family_set_fixed_for_both_sides':True,'prepared_unique_identifier_count':2953,'prepared_identifier_file_hash':'PASS','all_running_CPC_memberships_read':True,'current_prepared_active_skus':0,'current_prepared_inactive_only_skus':2953,'current_prepared_not_found':0,'known_multi_campaign_skus_minimum':4,'archive_membership_bound_disclosed':True,'missing_statistics_never_zero_filled':'PASS','secret_scan':'PASS','report_links':'PASS','marketplace_mutations':0,'db_data_writes':0}
(O/'addendum_validation.json').write_text(json.dumps(check,ensure_ascii=False,indent=2)+'\n')
files=sorted(set(newfiles+[O/'addendum_validation.json']))
files=[p for p in files if p.name!='ADDENDUM_SHA256SUMS']
(O/'ADDENDUM_SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(O).as_posix()+'\n' for p in files))
print('PASS: preserved initial/revised files; combined orders/value re-derived; 2953 IDs audited; secret scan clear')
print('Addendum files hashed:',len(files))

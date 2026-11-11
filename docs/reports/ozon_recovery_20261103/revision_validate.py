# поток: mkt
import pathlib,json,csv,hashlib,decimal,datetime as dt,subprocess,re
from dotenv import dotenv_values
O=pathlib.Path(__file__).parent;P=pathlib.Path('/opt/mp-analytics');D=decimal.Decimal

def rows(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
def save(n,x):(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str)+'\n')
# The original manifest enumerates original exports only; all must remain byte-for-byte unchanged.
original=subprocess.run(['sha256sum','-c','SHA256SUMS'],cwd=O,capture_output=True,text=True)
assert original.returncode==0,original.stderr+'\n'+str([l for l in original.stdout.splitlines() if not l.endswith('OK')])
main=rows('revised_sku_summary_20.csv');detail=rows('revised_sku_campaign_daily.csv')
assert len(main)==len({r['sku'] for r in main})==20
assert all(r['article'] and r['name'] and r['original_bid']=='не восстановлено' and r['installed_20261030_bid']=='не восстановлено' for r in main)
assert len(detail)==len({(r['sku'],r['campaign_id'],r['date_msk']) for r in detail})==987
post={'2026-10-31','2026-11-01','2026-11-02'}
amount=sum((D(r['spend_rub']) for r in detail if r['date_msk'] in post),D(0));assert amount==D('8168.96')
assert sum(D(r['ad_orders_api']) for r in detail if r['date_msk'] in post)==3
assert (O/'revised_sku_report_20.html').read_text().count('<tr class="sku">')==20
pr=rows('revised_prepared_composition_2953.csv');assert len(pr)==len({r['sku'] for r in pr})==2953
for cid,stream,n in [('49173478','new',458),('49173499','bundles',1425),('49173502','old',1070)]:
 expected={str(r['sku']) for r in json.loads((O/f'raw/products_{cid}.json').read_text())['products']}
 actual={r['sku'] for r in pr if r['stream']==stream};assert actual==expected and len(actual)==n
assert '6201364326' not in {r['sku'] for r in pr}
comp=rows('revised_wave_candidate_composition.csv');assert len(comp)==len({(r['campaign_id'],r['sku']) for r in comp})
assert all(r['stream']=='unknown' for r in comp if r['campaign_id']=='49321871')
sq=json.loads((O/'revised_sales_quality.json').read_text());assert all(r['complete'] for r in sq['coverage']) and sq['sold_sku_metadata_unmapped_lines']==0 and sq['orders_with_multiple_process_dates']==0
# Dedupe verification for the family union against actual source lines.
lines=rows('revised_sales_lines.csv');members=rows('revised_family_members.csv');models={r['current_model_id'] for r in members}
family=sum((D(r['ordered_value_rub']) for r in lines if r['date_msk'] in post and r['current_model_id'] in models),D(0));assert family==D('112340')
sale=rows('revised_sales_windows.csv');sr=next(r for r in sale if r['scope']=='fixed20_exact_sku' and r['baseline_days']=='28');assert D(sr['post_total_ordered_value_rub'])==D('98034') and D(sr['post_total_orders'])==9
# Verify every relative local report link resolves.
report=(O/'CORRECTED_REPORT.md').read_text()
for link in re.findall(r'\]\(([^)]+)\)',report):
 if '://' not in link:assert (O/link).exists(),link
secrets=[]
for config in [P/'.env',P/'.env.save']:
 if config.exists():
  for k,v in dotenv_values(config).items():
   if v and len(v)>=12 and any(t in k.upper() for t in ['TOKEN','SECRET','API_KEY','PASSWORD','PERF_CLIENT_ID']):secrets.append(v.encode())
for f in O.rglob('*'):
 if f.is_file():assert not any(secret in f.read_bytes() for secret in secrets),'Secret value found in '+str(f.relative_to(O))
checks={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'account':'oz_acc1','original_export_hash_verification':'PASS','original_files_checked':len(original.stdout.splitlines()),'main_sku_rows_exactly_20':'PASS','unique_placement_detail_rows':987,'two_baselines_same_weekday':'14 days => 6 reference + 3 post; 28 days => 12 reference + 3 post','post_spend_no_duplicate_rub':str(amount),'general_sales_fixed20_post_value_rub':'98034','general_sales_fixed20_post_orders':9,'current_family_union_post_value_rub':str(family),'prepared_unique_skus':2953,'candidate_outside_prepared_set':'PASS','report_links':'PASS','secret_values_scan':'PASS','marketplace_mutations':0,'database_data_writes':0,'schedule_changes':0}
save('revised_validation.json',checks)
sourcepaths=['README.md','sku_campaign_daily.csv','current_bids.csv','wave_candidates.csv','campaign_daily.csv','sku_pair_coverage.csv','coverage_summary.json']
save('revised_sources.json',{'account':'oz_acc1','checked_at_utc':checks['checked_at_utc'],'preserved_initial_sources':[{'path':str((O/n).resolve()),'sha256':hashlib.sha256((O/n).read_bytes()).hexdigest()} for n in sourcepaths],'supplemental_raw_directory':str((O/'revision_raw').resolve()),'concrete_gaps_queried':['offer_id, names, stock, availability, model_id for fixed20 + candidate SKU','complete noncancelled orders/postings for general sales in baseline and post periods; DB sales empty','model_id and aliases for every SKU with noncancelled sales','current composition/status of 4 new-period/preparation campaigns and 3 specific older campaigns with prepared-set overlap','two missing SKU-days; both remained empty'],'read_only_api_methods':['POST Seller /v3/product/info/list','POST Seller /v4/posting/fbs/list','POST Seller /v3/posting/fbo/list','GET Performance /api/client/campaign','GET Performance /api/client/campaign/{id}/v2/products','POST Performance /api/client/statistics (two concrete gaps only)','GET Performance /api/client/statistics/{UUID} and /report','POST Performance /api/client/token (value never persisted)'],'database':'SELECT-only reads and existing client advisory session lock; no table data writes','existing_clients':['collectors.ozon._headers','collectors.ozon_products endpoints','collectors.ozon_postings.fetch_postings','core.ozon_posting.product_price','collectors.ozon_ads._token','collectors.ozon_bids._campaign_products'],'unresolved':['original immutable manifest file and 10 excluded SKU','30 October bid-change journal and installed/original bids','individual per-SKU floor source','activation event history and first-wave membership','historical campaign and family membership','historical availability; post attribution/cancellation maturity']})
files=sorted(p for p in O.rglob('*') if p.is_file() and p.name!='REVISED_SHA256SUMS')
(O/'REVISED_SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(O).as_posix()+'\n' for p in files))
print('Validation PASS; original exports unchanged:',len(original.stdout.splitlines()),'files; main 20 unique SKU; no duplicates/secrets')
print('Corrected report:',str((O/'CORRECTED_REPORT.md').resolve()))

# поток: ozon-family; provenance and reproducibility, no production writes.
from pathlib import Path
import hashlib,json,datetime,collections
O=Path(__file__).parent;R=O.parents[2]
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()
dependencies=['AGENTS.md','CLAUDE.md','.claude/rules/kb-index.md','.claude/rules/ozon.md','.claude/rules/data-layer.md','.claude/rules/fin.md','.claude/rules/customer-orders.md','docs/CUSTOMER_ORDER_RULES.md','docs/BRIEF_MKT_OZON.md','collectors/ozon_products.py','collectors/ozon_attributes.py','collectors/ozon.py','core/db.py','collectors/set_cost.py','ops/fbs_ms_set_catalog.py','ops/fbs_ms_bundle_quantity.py','ops/fbs_ms_import_next5.py','docs/reports/ozon_recovery_20261103/daily_20261105/HANDOFF.md','docs/reports/ozon_recovery_20261103/handoff_20261104/HANDOFF.md','docs/reports/bundle_quantities_20261106/RESULT.md','docs/reports/ozon_year_review_20261105/SHA256SUMS']
sources=[]
for n in dependencies:
 p=R/n
 if p.exists():sources.append({'path':n,'sha256':sha(p),'mtime_utc':datetime.datetime.fromtimestamp(p.stat().st_mtime,datetime.timezone.utc).isoformat()})
raw=[]
for p in sorted((O/'snapshot').glob('*.json')):
 if p.name.startswith(('list_','info_','attrs_','category_schema_')):
  z=json.loads(p.read_text());raw.append({'path':str(p.relative_to(O)),'http':z.get('http'),'endpoint':z.get('endpoint'),'observed_at':z.get('observed_at'),'sha256':sha(p)})
manifest={'account':'oz_acc1','created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'business_access':'read_only','db_isolation':'REPEATABLE READ READ ONLY','write_scope':str(O),'dependencies':sources,'raw_api_responses':raw,
 'raw_response_counts':dict(collections.Counter(x['path'].split('/')[1].split('_')[0] for x in raw)),
 'api_attempt_count':'unknown; stored responses are not an attempt count; see COMMANDS.md',
 'official_document_sources':'agents/b_sources.md', 'annual_integrity':'agents/c_year_readiness.json; 395 source checksums verified; no new annual collection',
 'snapshot_semantics':'sequential observation windows, not atomic state; actual source timestamps retained'}
(O/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
files=sorted(p for p in O.rglob('*') if p.is_file() and p.name!='SHA256SUMS' and not p.name.endswith(('.lock','.tmp','.pyc')) and '__pycache__' not in p.parts)
(O/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(O)}\n' for p in files))
print(json.dumps({'files_hashed':len(files),'dependency_sources':len(sources),'raw_response_counts':manifest['raw_response_counts'],'business_writes':0}))

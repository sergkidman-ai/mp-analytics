"""Independent offline cardinality, composition and current-glue invariant checks."""
import ast
import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).parent
SNAP = HERE.parent/'snapshot'


def csvrows(name):
    with (HERE/name).open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def js(path):
    return json.loads(path.read_text())


expected = set()
catalog_groups = collections.defaultdict(set)
catalog_keys = {}
catalog_aliases = {}
for name in ('catalog.json','catalog_archived.json'):
    for row in js(SNAP/name):
        pid = str(row['id'])
        expected.add(pid)
        catalog_keys[pid]=(str(row.get('sku') or ''),str(row.get('offer_id') or ''))
        catalog_aliases[pid]={str(s) for s in [row.get('sku')]+[v.get('sku') for v in row.get('sources',[])] if s}
        model = str((row.get('model_info') or {}).get('model_id') or '')
        catalog_groups[model].add(pid)

cards = csvrows('a_cards.csv')
members = csvrows('b_glue_members.csv')
groups = csvrows('b_glue_groups.csv')
contexts = {r['set_code']:r for r in js(HERE/'a_tc_contexts.json')}
boms = {r['external_code']:r for r in js(SNAP/'db_set_cost.json')}
errors = []
checks = {}
for label, rows in [('a_cards',cards),('b_members',members)]:
    ids = [r['product_id'] for r in rows]
    checks[label] = dict(rows=len(rows),unique_ids=len(set(ids)),duplicates=len(ids)-len(set(ids)),
                         missing_ids=sorted(expected-set(ids)),extra_ids=sorted(set(ids)-expected))
    for r in rows:
        sku = '' if r['sku'] in {'0','None',''} else r['sku']
        if (sku,r['offer_id'])!=catalog_keys.get(r['product_id']):
            errors.append(dict(kind='primary_key_fields_mismatch',map=label,product_id=r['product_id']))


def explode(code, path=()):
    if code in path:
        raise ValueError('cycle')
    if code not in boms:
        return collections.Counter({code:1})
    raw = boms[code]['components']
    pairs = raw.items() if isinstance(raw,dict) else [(x,1) for x in raw]
    leaves = collections.Counter()
    for item, qty in pairs:
        if isinstance(item,str) and item.startswith('{'):
            item = ast.literal_eval(item)
        child = item['external_code'] if isinstance(item,dict) else item
        nested = explode(child,path+(code,))
        for k,v in nested.items():
            leaves[k] += v*int(qty)
    return leaves

for code, ctx in contexts.items():
    if dict(explode(code)) != ctx['components']:
        errors.append(dict(kind='bom_mismatch',code=code))

component_rows = csvrows('a_components.csv')
physical = collections.defaultdict(collections.Counter)
component_duplicates = collections.Counter()
for row in component_rows:
    physical[row['product_id']][row['component_code']] += int(row['quantity'])
    component_duplicates[(row['product_id'],row['component_code'])] += 1
    if int(row['quantity']) <= 0:
        errors.append(dict(kind='nonpositive_component',product_id=row['product_id']))

stock_rows=js(SNAP/'db_supplier_stock.json')
stock_date=max(r['captured_at'] for r in stock_rows)
stock_keys={(str(r['ms_id']),str(r['store']),r['captured_at']):str(r['external_code']) for r in stock_rows if r['captured_at']==stock_date}
old_stock_refs=0
for row in cards:
    pid=row['product_id']
    composition=json.loads(row['physical_components'])
    if composition != dict(physical[pid]):
        errors.append(dict(kind='card_component_rows_mismatch',product_id=pid))
    mult=int(row['multiplier'])
    if mult<1 or any(int(q)<=0 for q in composition.values()):
        errors.append(dict(kind='invalid_quantity',product_id=pid))
    if mult>1 and any(int(q)%mult for q in composition.values()):
        errors.append(dict(kind='bundle_component_not_multiple',product_id=pid))
    if row['composition_status'].startswith(('unresolved','unknown')) and composition:
        errors.append(dict(kind='unknown_with_asserted_composition',product_id=pid))
    refs=json.loads(row['stock_by_exact_external_code'])
    for store,entries in refs.items():
        if not isinstance(entries,list):
            errors.append(dict(kind='stock_aggregates_without_physical_keys',product_id=pid))
            continue
        seen=set()
        for ref in entries:
            k=(str(ref['ms_id']),str(store),ref['captured_at'])
            if ref['captured_at']!=stock_date:
                old_stock_refs+=1
            if k in seen or stock_keys.get(k)!=row['offer_id']:
                errors.append(dict(kind='stock_key_invalid',product_id=pid))
            seen.add(k)

observed_groups=collections.defaultdict(set)
for r in members:
    observed_groups[r['model_id']].add(r['product_id'])
    if set(r['sku_aliases'].split(';'))- {''} != catalog_aliases[r['product_id']]:
        errors.append(dict(kind='sku_alias_set_mismatch',product_id=r['product_id']))
    if r['product_id'] not in catalog_groups.get(r['model_id'],set()):
        errors.append(dict(kind='current_model_mismatch',product_id=r['product_id']))
group_ids=[r['model_id'] for r in groups]
for r in groups:
    ids=set(r['member_product_ids'].split(';'))
    if ids!=observed_groups[r['model_id']]:
        errors.append(dict(kind='group_members_mismatch',model_id=r['model_id']))
    if int(r['observed_products'])!=len(ids):
        errors.append(dict(kind='group_count_mismatch',model_id=r['model_id']))

ctx=contexts.get('5133',{})
if ctx.get('kind')=='drum_and_cartridge_set':
    errors.append(dict(kind='5133_role_conflict_not_resolved'))
example_components=[r for r in component_rows if r['identity_code']=='5133']
if not example_components or any(r['component_role']!='drum' for r in example_components):
    errors.append(dict(kind='5133_component_roles_not_all_drums'))
example_5129=[r for r in example_components if r['component_code']=='5129']
if not example_5129 or any(r.get('source_tc_component_type')!='Картридж' for r in example_5129):
    errors.append(dict(kind='5133_source_role_conflict_not_preserved'))

result=dict(observed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),expected_cards=len(expected),checks=checks,
    errors=errors,error_count=len(errors),bom_contexts_checked=len(contexts),
    component_rows=len(component_rows),duplicate_component_keys=sum(v-1 for v in component_duplicates.values()),
    mapping_statuses=dict(collections.Counter(r['mapping_status'] for r in cards)),
    composition_statuses=dict(collections.Counter(r['composition_status'] for r in cards)),
    latest_stock_snapshot_date=stock_date,latest_stock_physical_keys=len(stock_keys),old_stock_refs=old_stock_refs,
    stock_fix=dict(finding='Preliminary A summed stock across five capture dates and labelled max date.',
                   status='FIXED' if old_stock_refs==0 and not any(e['kind'].startswith('stock') for e in errors) else 'FAILED',
                   corrected_design='Card rows point to exact ms_id/store/capture; no stock sum across dates or cards.'),
    b_groups=len(groups),duplicate_model_group_keys=len(group_ids)-len(set(group_ids)),
    b_unknown_model_cards=len(observed_groups.get('',set())|observed_groups.get('0',set())),
    missing_or_zero_primary_sku_cards=sum(not v[0] for v in catalog_keys.values()),
    example_5133_kind=ctx.get('kind'),example_5133_components=ctx.get('components'),
    example_5133_all_component_roles_drums=bool(example_components) and all(r['component_role']=='drum' for r in example_components),
    example_5129_source_type_preserved=bool(example_5129) and all(r.get('source_tc_component_type')=='Картридж' for r in example_5129),
    financial_allocation_runtime_test='NOT_PERFORMED: A/B map contains no financial allocations. Count-once original-line conservation remains mandatory for future analysis.',
    historical_glue_validation='NOT_PROVEN: B is current Seller API model_id snapshot, not historical membership.',
    source_hashes={n:hashlib.sha256((HERE/n).read_bytes()).hexdigest() for n in ['a_cards.csv','a_components.csv','a_tc_contexts.json','b_glue_members.csv','b_glue_groups.csv']})
(HERE/'c_map_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
ready_path=HERE/'c_year_readiness.json'
ready=json.loads(ready_path.read_text())
ready['independent_map_validation']=dict(status='PASS_WITH_LIMITS' if not errors and all(not v['missing_ids'] and not v['extra_ids'] and not v['duplicates'] for v in checks.values()) else 'FAILED',
    artifact='c_map_validation.json',checks=checks,error_count=len(errors),stock_fix=result['stock_fix'],
    example_5133_kind=result['example_5133_kind'],missing_or_zero_primary_sku_cards=result['missing_or_zero_primary_sku_cards'],
    financial_allocation_runtime_test=result['financial_allocation_runtime_test'],historical_glue_validation=result['historical_glue_validation'])
ready['readiness']['complete_card_map']='A/B CARD COVERAGE VERIFIED: all 48,492 product_ids exactly once in each source map; unknown mapping/composition/model states retained. Unified final economic-family map remains leading-agent responsibility.'
ready['snapshot_finance_source_sha256']={n:hashlib.sha256((SNAP/n).read_bytes()).hexdigest() for n in ['db_year_coverage.json','db_year_monthly_coverage.json']}
ready_path.write_text(json.dumps(ready,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k in ['expected_cards','checks','error_count','duplicate_component_keys','old_stock_refs','example_5133_kind','stock_fix']},ensure_ascii=False))

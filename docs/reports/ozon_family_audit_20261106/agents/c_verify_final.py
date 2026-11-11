"""Independent final-map validation. Reads saved artifacts; writes C-only report."""
import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

HERE=Path(__file__).parent
ROOT=HERE.parent
SNAP=ROOT/'snapshot'

def rows(path):
    with path.open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))

def read(path):
    return json.loads(path.read_text())

def yes(x):
    return str(x).lower()=='true'

SOURCE_FILES=['cards.csv','components.csv','families.csv','glue_members.csv','exceptions.csv','build_map.py','map_summary.json','glue_groups.csv','glue_schemes.py','glue_schemes_summary.json']
initial_hashes={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCE_FILES}
cards=rows(ROOT/'cards.csv'); components=rows(ROOT/'components.csv')
families=rows(ROOT/'families.csv'); glue=rows(ROOT/'glue_members.csv'); exceptions=rows(ROOT/'exceptions.csv')
ac={r['product_id']:r for r in rows(HERE/'a_cards.csv')}
bm={r['product_id']:r for r in rows(HERE/'b_glue_members.csv')}
ctx={r['set_code']:r for r in read(HERE/'a_tc_contexts.json')}
fm={r['family_id']:r for r in families}
ms=collections.defaultdict(set)
for r in read(SNAP/'db_ms_product.json'):
    if not r['archived']:
        ms[str(r['external_code'])].add(str(r['ms_id']))
expected=set()
catalog_availability={}
for name in ('catalog.json','catalog_archived.json'):
    for r in read(SNAP/name):
        pid=str(r['id']); expected.add(pid)
        av=r.get('availabilities') or []
        catalog_availability[pid]=dict(states=sorted({x.get('availability','') for x in av}),
            by_source=[{k:x.get(k) for k in ['sku','source','availability','reasons']} for x in av],
            has_stock=(r.get('visibility_details') or {}).get('has_stock'),
            has_price=(r.get('visibility_details') or {}).get('has_price'))
failures=[]
def check(ok,kind,entity=''):
    if not ok: failures.append(dict(kind=kind,entity=entity))

check(len(cards)==len(expected)==48492,'card_count')
check(set(r['product_id'] for r in cards)==expected,'card_coverage')
check(len({r['product_id'] for r in cards})==len(cards),'duplicate_product_id')
check(len(fm)==len(families),'duplicate_family_id')
check(len({r['product_id'] for r in glue})==len(glue)==len(cards),'duplicate_or_missing_glue_rows')
check(set(r['product_id'] for r in glue)==expected,'glue_coverage')
ex_by_product=collections.defaultdict(set)
for r in exceptions:
    if r['scope']=='product_id': ex_by_product[r['entity_id']].add(r['exception_type'])

set_to_family={}
leaf_to_families=collections.defaultdict(set)
bf_sets=[]
for f in families:
    fid=f['family_id']; leaves=set(json.loads(f['component_parent_codes'])); parents=json.loads(f['set_parent_codes'])
    if f['family_kind']=='full_composition_context':
        check(f['boundary_status']=='composition_confirmed_boundary_proposed','false_confirmed_big_boundary',fid)
        check(len(leaves)>1,'full_family_not_heterogeneous',fid)
        bf_sets.append(tuple(sorted(leaves)))
        for parent in parents:
            check(parent in ctx and set(ctx[parent]['components'])==leaves,'family_full_leaf_set_mismatch',fid+':'+parent)
            check(parent not in set_to_family,'set_parent_multiple_families',parent)
            set_to_family[parent]=fid
        for leaf in leaves: leaf_to_families[leaf].add(fid)
    else:
        check(f['family_kind']=='parent_only_provisional' and f['boundary_status']=='not_a_confirmed_large_family','false_confirmed_provisional',fid)
        check(fid.startswith('PF:') and leaves=={fid[3:]} and not parents,'provisional_shape',fid)
check(len(bf_sets)==len(set(bf_sets)),'duplicate_full_leaf_family')
expected_set_parents={code for code,x in ctx.items() if len(x['components'])>1}
check(set(set_to_family)==expected_set_parents,'known_set_parents_discarded')

comp_by_product=collections.defaultdict(dict)
for r in components:
    pid,code=r['product_id'],r['component_external_code']
    check(pid in expected,'orphan_component',pid)
    check(code not in comp_by_product[pid],'duplicate_component',pid+':'+code)
    comp_by_product[pid][code]=int(r['quantity_per_card'])
    check(int(r['quantity_per_card'])>0,'nonpositive_component',pid)
    actual=set(json.loads(r['component_ms_ids']))
    check(actual==ms[code],'component_ms_ids_mismatch',pid+':'+code)
    check(r['component_ms_link_status']==('exact_live_ms_external_code' if ms[code] else 'not_found_live_ms'),'component_ms_status_mismatch',pid+':'+code)

assigned=collections.Counter(); assigned_current=collections.Counter(); candidates_counts=collections.Counter()
count=collections.Counter()
for r in cards:
    pid=r['product_id']; a=ac[pid]; b=bm[pid]
    physical=json.loads(a['physical_components']); parent=a['identity_code'] if physical else ''
    fid=r['large_family_id']; candidates=set(json.loads(r['candidate_family_ids']))
    check(r['offer_id']==a['offer_id'] and r['ozon_sku']==a['sku'],'card_source_identity_mismatch',pid)
    check(r['ozon_sku_aliases']==b['sku_aliases'],'alias_source_mismatch',pid)
    check(r['model_id']==b['model_id'],'model_source_mismatch',pid)
    availability=catalog_availability[pid]
    check(json.loads(r['availability_states'])==availability['states'],'availability_states_raw_mismatch',pid)
    check(json.loads(r['availability_by_source'])==availability['by_source'],'availability_by_source_raw_mismatch',pid)
    check(yes(r['any_source_available'])==('AVAILABLE' in availability['states']),'any_source_available_raw_mismatch',pid)
    check(yes(r['any_source_hidden'])==('HIDDEN' in availability['states']),'any_source_hidden_raw_mismatch',pid)
    for target,key in [('visibility_has_stock','has_stock'),('visibility_has_price','has_price')]:
        check(r[target]==('' if availability[key] is None else str(availability[key])),'visibility_raw_mismatch',pid+':'+target)
    if not b['schema_source']:
        count['missing_schema_source']+=1
        check('category_schema_unavailable' in ex_by_product[pid],'missing_schema_exception',pid)
    check(r['specification_parent_code']==parent,'specification_parent_mismatch',pid)
    check(r['parent_group_id']==('PG:'+parent if parent else ''),'parent_group_mismatch',pid)
    wh=parent if parent and ms[parent] else ''
    check(r['warehouse_parent_code']==wh,'false_or_missing_warehouse_parent',pid)
    expected_wh='exact_live_ms_external_code' if wh else 'no_live_ms_code' if parent else 'unresolved'
    check(r['warehouse_link_status']==expected_wh,'warehouse_status_mismatch',pid)
    if parent and not wh:
        count['no_live_ms_parent']+=1
        check('no_live_ms_parent_record' in ex_by_product[pid],'missing_ms_exception',pid)
    expected_comps=physical if int(r['bundle_multiplier'])>1 or len(physical)>1 else {}
    check(comp_by_product[pid]==expected_comps,'missing_or_extra_physical_components',pid)
    if int(r['bundle_multiplier'])>1:
        check(all(q%int(r['bundle_multiplier'])==0 for q in expected_comps.values()),'bundle_multiplicity_mismatch',pid)
    if not physical:
        count['physical_unknown']+=1
        count['current_physical_unknown']+=r['catalog_scope']=='current_nonarchived'
        check(not fid and not candidates and r['family_status']=='unresolved_physical_mapping','unknown_promoted_to_family',pid)
        check('physical_link_unresolved' in ex_by_product[pid],'missing_physical_unknown_exception',pid)
    elif parent in set_to_family:
        check(fid==set_to_family[parent] and candidates=={fid} and r['family_status']=='confirmed_composition_context','set_direct_family_mismatch',pid)
    else:
        direct=leaf_to_families[parent]
        check(candidates==direct,'transitive_or_missing_candidate_family',pid)
        if len(direct)>1:
            count['ambiguous_shared_component']+=1
            check(not fid and r['family_status']=='ambiguous_shared_component','ambiguous_assigned_or_lost',pid)
            check('shared_component_family_boundary' in ex_by_product[pid],'missing_shared_component_exception',pid)
        elif len(direct)==1:
            check(fid==next(iter(direct)) and r['family_status']=='unique_direct_component_context','unique_component_family_mismatch',pid)
        else:
            check(fid=='PF:'+parent and r['family_status']=='parent_only_provisional','parent_only_false_confirmed',pid)
    if fid:
        check(fid in fm,'orphan_family',pid)
        assigned[fid]+=1
        assigned_current[fid]+=r['catalog_scope']=='current_nonarchived'
    candidates_counts.update(candidates)
    if r['glue_status']=='unknown':
        count['unknown_glue']+=1
        check(not r['model_id'],'unknown_glue_positive_model',pid)
for f in families:
    fid=f['family_id']
    check(int(f['assigned_cards'])==assigned[fid],'family_assigned_count_mismatch',fid)
    check(int(f['assigned_current_cards'])==assigned_current[fid],'family_current_count_mismatch',fid)
    check(int(f['candidate_cards'])==candidates_counts[fid],'family_candidate_count_mismatch',fid)
for r in glue:
    check(r['historical_membership_verified']=='false' and r['storefront_verified']=='false','false_glue_history_or_storefront',r['product_id'])
check(any(r['exception_type']=='incomplete_year_economics' for r in exceptions),'annual_incomplete_exception_missing')
summary=read(ROOT/'map_summary.json')
check(summary['current']['physical_link_unknown']==count['current_physical_unknown']==1157,'unknown_current_count_mismatch')
check(summary['family_context_rows']==len(bf_sets)==987,'full_family_count_mismatch')
check(summary['provisional_family_rows']==sum(f['family_kind']=='parent_only_provisional' for f in families)==2024,'provisional_family_count_mismatch')
check(summary['all']['unknown_physical_or_family_or_glue']==sum(not r['parent_group_id'] or r['family_status'] in {'ambiguous_shared_component','parent_only_provisional'} or r['glue_status']=='unknown' for r in cards),'unknown_union_summary_mismatch')
card_by_alias={}
for c in cards:
    for alias in c['ozon_sku_aliases'].split(';'):
        if alias:
            check(alias not in card_by_alias or card_by_alias[alias]['product_id']==c['product_id'],'ambiguous_final_alias',alias)
            card_by_alias[alias]=c
year_key_readiness={}
year_root=ROOT.parent/'ozon_year_review_20261105'
for name in ['sku_general_sales_daily_year.csv','sku_positive_cpc_daily_year.csv']:
    with (year_root/name).open(encoding='utf-8-sig') as f:
        keys={r['sku'] for r in csv.DictReader(f)}
    totals=collections.Counter()
    statuses=collections.Counter()
    product_ids=set()
    for sku in keys:
        c=card_by_alias.get(sku)
        if not c:
            totals['missing_map_alias']+=1
            continue
        product_ids.add(c['product_id'])
        statuses[c['family_status']]+=1
        totals['physical_known']+=bool(c['parent_group_id'])
        totals['BF_assigned']+=c['large_family_id'].startswith('BF-')
        totals['PF_provisional']+=c['family_status']=='parent_only_provisional'
        totals['shared_ambiguous']+=c['family_status']=='ambiguous_shared_component'
        totals['physical_unknown']+=not c['parent_group_id']
    check(not totals['missing_map_alias'],'annual_key_missing_final_map',name)
    year_key_readiness[name]=dict(unique_skus=len(keys),unique_product_ids=len(product_ids),**totals,family_statuses=dict(statuses))

card_groups=collections.defaultdict(list)
for c in cards:
    if c['model_id']:card_groups[c['model_id']].append(c)
b_diagnostics=collections.defaultdict(set)
for r in rows(HERE/'b_exceptions.csv'):
    if r['scope']=='model_id':b_diagnostics[r['key']].add(r['exception_type'])
glue_groups=rows(ROOT/'glue_groups.csv')
check({r['model_id'] for r in glue_groups}=={mid for mid,cs in card_groups.items() if len(cs)>1},'multimember_glue_group_coverage')
for g in glue_groups:
    cs=card_groups[g['model_id']]
    pgs={c['parent_group_id'] for c in cs if c['parent_group_id']}
    unknown=sum(not c['parent_group_id'] for c in cs)
    narrow_ok=not unknown and len(pgs)==1 and not b_diagnostics[g['model_id']]
    check((g['classification']=='корректно по доступным структурным данным')==narrow_ok,'false_or_missing_narrow_glue_positive',g['model_id'])
    check(int(g['cards'])==len(cs) and int(g['unknown_physical_cards'])==unknown,'glue_scheme_counts',g['model_id'])
final_hashes={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCE_FILES}
check(initial_hashes==final_hashes,'source_files_changed_during_validation')
availability_counts={}
for scope in ['all','current_nonarchived','archived']:
    subset=cards if scope=='all' else [r for r in cards if r['catalog_scope']==scope]
    availability_counts[scope]=dict(cards=len(subset),
        state_combinations=dict(collections.Counter('+'.join(json.loads(r['availability_states'])) or 'NO_STATE' for r in subset)),
        any_source_available=sum(yes(r['any_source_available']) for r in subset),
        without_available_source=sum(not yes(r['any_source_available']) for r in subset),
        any_source_hidden=sum(yes(r['any_source_hidden']) for r in subset),
        mixed_available_hidden=sum(yes(r['any_source_available']) and yes(r['any_source_hidden']) for r in subset))
result=dict(status='PASS_WITH_LIMITS' if not failures else 'FAIL',checked_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
    failure_count=len(failures),failures=failures,counts=dict(count),cards=len(cards),families=len(families),components=len(components),
    full_composition_contexts=len(bf_sets),provisional_contexts=sum(f['family_kind']=='parent_only_provisional' for f in families),
    known_heterogeneous_set_parents_expected=len(expected_set_parents),known_heterogeneous_set_parents_mapped=len(set_to_family),
    annual_key_family_readiness=year_key_readiness,
    availability_validation=dict(status='RAW_FIELDS_VERIFIED',counts=availability_counts,
        interpretation='Availability is per source/channel. AVAILABLE+HIDDEN is not a wholly hidden card. Counts by flags overlap; do not add them.'),
    glue_schemes=dict(multimember_groups=len(glue_groups),classifications=dict(collections.Counter(r['classification'] for r in glue_groups)),
        interpretation='Narrow structural positive only: same one confirmed parent and zero B diagnostics; not storefront or historical proof.'),
    checks=['All 48,492 product_ids exactly once in cards and glue; source identity/alias/model matches.',
    'All heterogeneous known BOM parents retained in exact full-leaf-set context; no shared-leaf transitive closure.',
    'Ambiguous shared components have no assigned family and explicit exception; each card has at most one assigned family.',
    'PF boundary explicitly provisional; BF boundary proposed composition context, not asserted complete business taxonomy.',
    'All physical unknown and absent-live-MS parents have explicit product exceptions.',
    'Components preserve A quantities/multiplicity; exact live MS ids and warehouse parent checked against source.',
    'Family member/candidate counts recomputed; current unknown physical count independently confirmed.',
    'Historical/storefront membership false; incomplete annual financial economics explicitly recorded.'],
    limitations=['Financial allocations are absent from mapping; runtime revenue conservation not tested. Future revenue must use original posting lines once, never component expansion.',
    'Families are contextual proposed boundaries; candidate family totals are nonadditive. Shared stock must deduplicate ms_id/store/capture across card references.',
    'No historical family reconstruction, no causal/effect analysis, no full-year financial readiness claim.',
    'One single-component BOM reference has no expanded root component row by documented output policy; A source retains physical component and the card remains mapped.'],
    source_hashes=final_hashes,source_files_stable_during_validation=initial_hashes==final_hashes)
(HERE/'c_final_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
ready_path=HERE/'c_year_readiness.json'
ready=read(ready_path)
ready['unified_final_map_validation']=dict(status=result['status'],artifact='c_final_validation.json',failure_count=len(failures),annual_key_family_readiness=year_key_readiness,
    availability_validation=result['availability_validation'])
ready_path.write_text(json.dumps(ready,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k in ['status','failure_count','counts','cards','families','components','known_heterogeneous_set_parents_expected','known_heterogeneous_set_parents_mapped']},ensure_ascii=False))

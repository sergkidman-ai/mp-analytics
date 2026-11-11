# поток: ozon-family; independent leading-agent checks against saved raw sources.
from pathlib import Path
import json,csv,collections,datetime
O=Path(__file__).parent;S=O/'snapshot'
def rows(p):return list(csv.DictReader(p.open(encoding='utf-8-sig')))
cards=rows(O/'cards.csv');by_offer={c['offer_id']:c for c in cards};by_pid={c['product_id']:c for c in cards}
comp=collections.defaultdict(dict)
for c in rows(O/'components.csv'):comp[c['offer_id']][c['component_external_code']]=int(c['quantity_per_card'])
bom={r['external_code']:r['components'] for r in json.loads((S/'db_set_cost.json').read_text())}
models={r['external_code']:r for r in json.loads((S/'db_prc_tc_model.json').read_text())}
checks={
 'all_cards_unique':len(cards)==len(by_pid)==48492,
 'leading_zero_parent_and_child':by_offer['0001']['specification_parent_code']==by_offer['00011']['specification_parent_code']=='0001',
 'bundle_0088X2_two_same_units':comp['0088X2']=={'0088':2} and by_offer['0088X2']['fulfillment_type']=='homogeneous_bundle',
 'color_set_3069_exact_source':comp['3069']==dict(collections.Counter(bom['3069']))=={'3065':1,'3066':1,'3067':1,'3068':1},
 'color_set_0372_exact_source':comp['0372']==dict(collections.Counter(bom['0372']))=={'0411':1,'0412':1,'0413':1,'0414':1},
 'mixed_6642_kept_in_family':comp['6642']==dict(collections.Counter(bom['6642']))=={'5425':1,'4308':1,'4309':1,'4310':1,'4311':1} and by_offer['6642']['large_family_id'].startswith('BF-'),
 '6642_not_brother': 'Konica Minolta' in by_offer['6642']['name'],
 'drum_set_5133_not_toner_pair':by_offer['5133']['fulfillment_type']=='heterogeneous_drum_set' and models['5133']['consumable_type']=='Комплект фотобарабанов',
 'source_5129_conflict_not_silently_changed':models['5129']['consumable_type']=='Картридж',
 'mixed_seo_not_forced_to_anchor_bom':all(not by_offer[x]['specification_parent_code'] and x not in comp for x in ['66281','66391']),
 'archived_00331_color_conflict_not_black_execution':by_offer['00331']['ozon_color']=='M' and not by_offer['00331']['specification_parent_code'],
 'historical_6452_suffix_not_added_to_catalog':'6452X3MS22I0' not in by_offer,
 'zero_model_not_common_glue':all(not c['model_id'] for c in cards if c['glue_status']=='unknown'),
 'shared_component_no_arbitrary_assignment':all(not c['large_family_id'] and len(json.loads(c['candidate_family_ids']))>1 for c in cards if c['family_status']=='ambiguous_shared_component')}
raw=[]
for n in ['catalog.json','catalog_archived.json']:raw.extend(json.loads((S/n).read_text()))
group=[x for x in raw if (x.get('model_info') or {}).get('model_id')==223693553]
checks['one_count_gap_from_raw']=len(group)==8 and {x['model_info']['count'] for x in group}=={9}
checks['current_visibility_not_collapsed']=sum(c['catalog_scope']=='current_nonarchived' and c['any_source_available']=='True' and c['any_source_hidden']=='True' for c in cards)==4387
out={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':checks,'failure_count':sum(not v for v in checks.values()),'method':'leading agent direct checks of required examples against raw snapshot, independent of report text'}
(O/'leader_validation.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps({'checks':len(checks),'failures':out['failure_count']}));assert all(checks.values())

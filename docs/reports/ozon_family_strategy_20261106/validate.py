# поток: ozon-family-strategy; independent accounting and scope checks.
from common import *
import collections
cat,original,alias=current();cs=list(rows(O/'cards.csv'));byid={c['product_id']:c for c in cs};byoffer={c['offer_id']:c for c in cs}
fs=list(rows(O/'families.csv'));fam={f['family_id']:f for f in fs};defs=read(O/'family_definitions.json');candidate_ids={c['candidate_family_id'] for c in rows(O/'parent_family_candidates.csv')}
h=read(O/'history_by_card.json');lines=list(rows(O/'sales_lines.csv'));checks={}
def check(name,v):checks[name]=bool(v)
check('scope_exactly_current_nonarchived',len(cs)==len(byid)==22536 and set(byid)==set(cat) and all(c['is_archived']=='False' for c in cs))
seenalias={};alias_errors=[]
for c in cs:
 expected={k for k in [cat[c['product_id']].get('sku')]+[s.get('sku') for s in cat[c['product_id']].get('sources',[])] if k}
 actual=set(json.loads(c['ozon_sku_aliases']))
 if {str(k) for k in expected}!=actual:alias_errors.append(c['product_id'])
 for k in actual:
  if k in seenalias and seenalias[k]!=c['product_id']:alias_errors.append(k)
  seenalias[k]=c['product_id']
check('all_confirmed_aliases_no_collisions',not alias_errors and seenalias==alias)
check('all_cards_have_assignment_or_explicit_question',all((bool(c['family_id']) and c['family_status']=='assigned_corroborated_series' and c['family_basis'] and c['family_source']) or (not c['family_id'] and c['family_status']=='requires_clarification' and c['question_group']) for c in cs))
check('candidate_ids_have_definitions',all(set(json.loads(c['candidate_family_ids']))<=set(fam)|candidate_ids for c in cs))
check('no_proposed_candidate_allocations',all(not c['family_id'].startswith('C-') for c in cs))
check('no_false_parent_only_finalization',all(not c['family_id'] for c in cs if c['family_basis']=='parent_group_only_not_final_big_family'))
glue=collections.Counter(c['model_id'] for c in cs)
check('glue_counts_current_members_only',all(int(c['glue_current_member_count'])==glue[c['model_id']] and int(c['glue_api_reported_count'])==cat[c['product_id']]['model_info']['count'] for c in cs))
ac={r['product_id']:r for r in rows(AUD/'agents/a_cards.csv') if r['product_id'] in cat};rmap={r['product_id']:r for r in rows(O/'physical_resolution.csv')}
physbad=[]
for c in cs:
 pid=c['product_id'];old=json.loads(ac[pid]['physical_components']);now=json.loads(c['physical_components'])
 if old and old!=now:physbad.append(pid)
 if not old and now:
  r=rmap[pid]
  if r['resolved']!='True' or r['reasons']!='[]' or now!={r['physical_parent_code']:1}:physbad.append(pid)
 if any(not isinstance(q,int) or q<=0 for q in now.values()):physbad.append(pid)
 if c['fulfillment_type']=='homogeneous_bundle' and (len(now)!=1 or sum(now.values())!=int(c['bundle_multiplier'])):physbad.append(pid)
check('exact_compositions_and_bundle_multiplicity_preserved',not physbad)
rawbom={r['external_code']:r['components'] for r in read(S/'db_set_cost.json')}
def expand(code,trail=()):
 assert code not in trail
 if code not in rawbom:return collections.Counter({code:1})
 v=rawbom[code];parts=collections.Counter(v) if isinstance(v,list) else collections.Counter(v);out=collections.Counter()
 for child,n in parts.items():
  for leaf,q in expand(child,trail+(code,)).items():out[leaf]+=q*n
 return out
check('physical_sets_match_original_recursive_BOM',all(json.loads(c['physical_components'])=={k:v*int(c['bundle_multiplier']) for k,v in expand(c['physical_parent_code']).items()} for c in cs if c['physical_parent_code'] in rawbom))
comp=collections.defaultdict(dict);componentkeys=set()
for c in rows(O/'components.csv'):
 key=(c['product_id'],c['component_external_code']);assert key not in componentkeys;componentkeys.add(key);comp[c['product_id']][c['component_external_code']]=int(c['quantity_per_market_unit'])
check('component_layer_consistent',all(comp[c['product_id']]==json.loads(c['physical_components']) for c in cs if len(json.loads(c['physical_components']))>1 or int(c['bundle_multiplier'])>1))
check('components_without_current_ozon_cards_retained',any(c['component_own_current_ozon_card']=='False' for c in rows(O/'components.csv')))
linekeys=set();orderids=collections.defaultdict(set);sales=collections.Counter();units=collections.Counter()
for x in lines:
 k=(x['scheme'],x['posting_number'],x['line_index']);assert k not in linekeys;linekeys.add(k)
 assert x['product_id'] in cat and alias[x['sku']]==x['product_id'] and START<=x['date_msk']<=END
 c=byid[x['product_id']];bucket=c['family_id'] or 'UNASSIGNED';sales[bucket]+=int(x['ordered_value_kopeks']);units[bucket]+=int(x['market_units']);orderids[bucket].add(x['order_id'])
check('family_orders_count_original_ids',all(int(f['annual_unique_orders'])==len(orderids[f['family_id']]) for f in fs))
check('family_value_and_units_from_each_line_once',all(cents(f['ordered_value_rub'])==sales[f['family_id']] and int(f['market_units'])==units[f['family_id']] for f in fs))
cpc=collections.Counter()
for c in cs:cpc[c['family_id'] or 'UNASSIGNED']+=h[c['product_id']]['cpc_cents']
check('CPC_once_per_card_allocated_or_unassigned',all(cents(f['cpc_spend_rub'])==cpc[f['family_id']] for f in fs))
base=read(O/'history_reconciliation.json')
check('money_and_units_conserved_to_filtered_original_sources',sum(sales.values())==base['selected_ordered_value_cents'] and sum(cpc.values())==base['selected_cpc_cents'] and sum(units.values())==base['selected_market_units'])
check('raw_normalized_reconciliation_passed',base['sales_vs_normalized_per_card_mismatches']==base['cpc_vs_original_reports_per_card_mismatches']==0)
qm=list(rows(O/'clarification_members.csv'));qids={r['product_id'] for r in qm};qs=list(rows(O/'clarification_queue.csv'))
check('clarification_current_disjoint_complete',len(qm)==len(qids) and qids=={c['product_id'] for c in cs if not c['family_id']} and qids<=set(cat))
check('queue_money_equals_unassigned',sum(cents(q['ordered_value_rub']) for q in qs)==sales['UNASSIGNED'] and sum(cents(q['cpc_spend_rub']) for q in qs)==cpc['UNASSIGNED'])
check('no_forced_novel_or_zero_sales_tail',all(f['demand_class']!='длинный хвост' for f in fs if int(f['annual_unique_orders'])==0 or int(f['observed_age_days'])<365))
check('simple_demand_thresholds',all((int(f['annual_unique_orders'])>=110 if f['demand_class']=='бестселлер' else 1<=int(f['annual_unique_orders'])<=3 if f['demand_class']=='длинный хвост' else 4<=int(f['annual_unique_orders'])<=109 if f['demand_class']=='обычный спрос' else True) for f in fs))
check('missing_financial_profit_not_zero',all(f['annual_financial_profit']=='' for f in fs))
check('0005_6629_no_automatic_shared_component_union',json.loads(byoffer['0005']['physical_components'])=={'1327':1,'1328':1,'1329':1,'1330':1} and json.loads(byoffer['6629']['physical_components'])=={'1327':1,'1328':1,'1329':1,'1330':1,'2985':1} and not byoffer['6629']['family_id'])
check('6642_known_mixed_set_retained_unassigned_not_duplicated',len(json.loads(byoffer['6642']['physical_components']))==5 and not byoffer['6642']['family_id'] and len(json.loads(byoffer['6642']['candidate_family_ids']))==2)
check('mixed_seo_physical_not_invented',all(not byoffer[o]['physical_parent_code'] and json.loads(byoffer[o]['physical_components'])=={} for o in ['66281','66391']))
check('ordinary_multicolor_03066_not_set_or_black_proxy',byoffer['03066']['physical_parent_code']=='0306' and byoffer['03066']['fulfillment_type']=='single')
check('standalone_seo_in_family_without_glue_requirement',any(c['seo_flag']=='True' and c['glue_status']=='api_singleton' and c['family_id'] for c in cs))
out={'checked_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':checks,'failure_count':sum(not x for x in checks.values()),
 'current_cards':len(cs),'family_rows':len(fs),'alias_count':len(alias),'assigned_cards':sum(bool(c['family_id']) for c in cs),'unassigned_cards':len(qids),
 'source_hashes':{n:sha(O/n) for n in ['cards.csv','components.csv','families.csv','clarification_queue.csv','clarification_members.csv','history_reconciliation.json','allocation_reconciliation.json','demand_rule.json']},
 'status':'PASS_WITH_OPEN_BUSINESS_QUESTIONS' if all(checks.values()) else 'FAIL'}
save('validation.json',out);print(js({'checks':len(checks),'failures':out['failure_count'],'status':out['status']}));assert not out['failure_count'],[k for k,v in checks.items() if not v]

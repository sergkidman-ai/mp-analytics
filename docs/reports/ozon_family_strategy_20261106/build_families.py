# поток: ozon-family-strategy; corroborated series, separate physical/BOM/glue layers.
from common import *
import collections
cat,old,alias=current();metrics=read(O/'history_by_card.json')
aliases_by_pid=collections.defaultdict(list)
for sku,pid in alias.items():aliases_by_pid[pid].append(sku)
current_offers={c['offer_id'] for c in old.values()};db_observed_at=read(S/'db_manifest.json')['started_at']
glue_counts=collections.Counter(str((c.get('model_info') or {}).get('model_id') or '') for c in cat.values())
models={x['external_code']:x for x in read(S/'db_prc_tc_model.json')}
context={x['set_code']:x for x in read(AUD/'agents/a_tc_contexts.json') if x['set_code'] in {c['offer_id'] for c in old.values()}}
ac={x['product_id']:x for x in rows(AUD/'agents/a_cards.csv') if x['product_id'] in cat}
resolution={x['product_id']:x for x in rows(O/'physical_resolution.csv')}
evidence=list(rows(O/'composition_boundaries.csv'));extensions=list(rows(O/'mixed_extensions.csv'))
owner=read(O/'owner_rules.json') if (O/'owner_rules.json').exists() else {'mixed_full_series_plus_drum':'pending','source':'unanswered clarification; do not infer approval'}
def profile(code):return tuple(sorted({(str(x.get('brand','')).strip().casefold(),str(x.get('title','')).strip().casefold()) for x in models.get(code,{}).get('printer_models') or []}))
def role(code):
 t=models.get(code,{}).get('consumable_type','');return 'cartridge' if t in ['Картридж','Тонер-картридж'] else 'drum' if t=='Фотобарабан' else t
def key(code):return (models.get(code,{}).get('united_title') or '',profile(code),role(code))
families={};familykey={};codefamilies=collections.defaultdict(set)
for e in evidence:
 if e['corroborated_series_core']!='True':continue
 code=e['set_code'];leaves=sorted(context[code]['components']);k=key(leaves[0]);fid='F-'+hashlib.sha256(js(k).encode()).hexdigest()[:12]
 familykey[k]=fid
 f=families.setdefault(fid,{'family_id':fid,'family_name':k[0],'functional_role':k[2],'printer_profile':k[1],'parent_codes':set(),'composition_codes':set(),
 'membership_basis':'explicit TC series + identical full printer profile + same functional role + confirmed complete BOM; no transitive common-part merge',
 'boundary_status':'corroborated_series_boundary; broader cross-role boundary requires clarification','related_question_ids':set()})
 f['parent_codes'].update([code]+leaves);f['composition_codes'].add(code)
# Additional exact physical parents in this already BOM-corroborated series.
physical={}
for pid,c in old.items():
 a=ac[pid];pc=c['specification_parent_code'];composition=json.loads(a['physical_components']);ptype=c['fulfillment_type'];basis=c['link_status'];source=c['physical_source'];at=c['physical_observed_at']
 if not pc and resolution[pid]['resolved']=='True':
  r=resolution[pid];pc=r['physical_parent_code'];composition={pc:1};ptype='single';basis=r['rule'];source=r['evidence_source'];at=r['observed_at']
 physical[pid]={'code':pc,'components':composition,'type':ptype,'basis':basis,'source':source,'at':at}
 if pc and pc not in context and key(pc) in familykey:families[familykey[key(pc)]]['parent_codes'].add(pc)
for fid,f in families.items():
 for code in f['parent_codes']:codefamilies[code].add(fid)
proposals=[];extension_candidates=collections.defaultdict(set)
for x in extensions:
 baseids=codefamilies[x['core_set']];assert len(baseids)==1
 fid=next(iter(baseids));extra=json.loads(x['extra_drum_codes']);other=set().union(*(codefamilies[d] for d in extra))-{fid}
 proposal={'core_family_id':fid,'core_set':x['core_set'],'mixed_set':x['mixed_set'],'drum_codes':x['extra_drum_codes'],'other_direct_series_families':js(sorted(other)),
  'physical_composition_confirmed':True,'full_containment_and_compatibility_corroborated':x['corroborated'],'business_boundary_status':'requires_owner_confirmation' if owner['mixed_full_series_plus_drum']=='pending' else 'owner_rule_requires_individual_conflict_check',
  'proposed_action':'one family for complete toner series plus confirmed drum; keep all physical codes and BOM separate',
  'evidence_source':'mixed_extensions.csv; composition_boundaries.csv; prior exact BOM and TC full printer_profiles','question_id':'Q-MIXED-BOUNDARY'}
 proposals.append(proposal);extension_candidates[x['mixed_set']].add(fid);extension_candidates[x['mixed_set']].update(other)
 families[fid]['related_question_ids'].add('Q-MIXED-BOUNDARY')
 # No inferred answer. Even an accepted general rule cannot silently resolve conflicts with another complete series.
 if owner['mixed_full_series_plus_drum']=='accepted' and x['corroborated']=='True' and not other:
  families[fid]['parent_codes'].update([x['mixed_set']]+extra);families[fid]['composition_codes'].add(x['mixed_set'])
  for code in [x['mixed_set']]+extra:codefamilies[code].add(fid)
ms=collections.defaultdict(list)
for x in read(S/'db_ms_product.json'):
 if not x['archived']:ms[x['external_code']].append(x['ms_id'])
out=[];compositionrows=[]
for pid,c in old.items():
 p=physical[pid];pc=p['code'];candidate=set(codefamilies.get(pc,set())) if pc else set(codefamilies.get(c['code_anchor'],set()))
 candidate.update(extension_candidates.get(pc,set()))
 fid='';status='requires_clarification';why='';qid='';cross=False
 if not pc:why='physical_execution_unknown';qid='Q-PHYSICAL'
 elif pc in extension_candidates and not codefamilies[pc]:why='mixed_set_boundary_pending';qid='Q-MIXED-BOUNDARY'
 elif len(codefamilies[pc])==1:
  fid=next(iter(codefamilies[pc]));status='assigned_corroborated_series';why=families[fid]['membership_basis'];candidate={fid}
 elif len(codefamilies[pc])>1:why='shared_component_multiple_series';qid='Q-SHARED'
 elif len(p['components'])>1:
  for code in p['components']:candidate.update(codefamilies[code])
  cross=len(candidate)>1;why='interfamily_set_boundary_pending' if cross else 'composition_does_not_establish_business_boundary';qid='Q-INTERFAMILY' if cross else 'Q-COMPOSITION-BOUNDARY'
 else:why='parent_group_only_not_final_big_family';qid='Q-PARENT-BOUNDARY'
 if qid:
  for candidateid in candidate:families[candidateid]['related_question_ids'].add(qid)
 # Always one nonadditive analysis bucket. Unassigned rows are never credited to candidates.
 bucket=fid or ('UNRESOLVED:'+pc if pc else 'UNRESOLVED-CARD:'+pid)
 aliases=sorted(aliases_by_pid[pid])
 row={'product_id':pid,'offer_id':c['offer_id'],'ozon_sku':c['ozon_sku'],'ozon_sku_aliases':js(aliases),'name':c['name'],
  'snapshot_date':'2026-11-06','is_archived':False,'created_at':cat[pid]['created_at'],'code_anchor':c['code_anchor'],
  'physical_parent_code':pc,'parent_group_id':'PG:'+pc if pc else '', 'physical_status':p['basis'] if pc else 'requires_clarification',
  'physical_components':js(p['components']),'fulfillment_type':p['type'],'bundle_multiplier':c['bundle_multiplier'],
  'seo_flag':c['seo_flag'],'seo_suffix_candidate':c['seo_suffix_candidate'],
  'family_id':fid,'family_status':status,'family_basis':why,'candidate_family_ids':js(sorted(candidate)),'analysis_bucket':bucket,
  'interfamily_set_status':'candidate_pending_accounting_rule' if cross else '',
  'model_id':c['model_id'],
  'glue_status':'multiple_current_members' if glue_counts[c['model_id']]>1 else 'api_singleton' if cat[pid]['model_info']['count']==1 else 'one_current_member_broader_api_group',
  'glue_current_member_count':glue_counts[c['model_id']],'glue_api_reported_count':cat[pid]['model_info']['count'],
  'glue_scope':'current snapshot members only; no historical membership assertion','availability_states':c['availability_states'],
  'warehouse_ms_matches':len(ms[pc]) if pc else '', 'cogs_status':'shipment_specific_FIFO_bridge_required; not_computed' if pc and len(ms[pc])>1 else 'not_computed_no_zero_fill',
  'warehouse_resolution_status':('multiple_live_codes_require_actual_shipment_ms_id' if len(ms[pc])>1 else 'one_live_code_not_proof_of_specific_shipment' if len(ms[pc])==1 else 'no_live_exact_ms_record') if pc else 'physical_parent_unknown',
  'question_group':qid,'card_source':c['card_source'],'card_observed_at':c['observed_at'],'physical_source':p['source'],'physical_observed_at':p['at'],
  'family_source':'composition_boundaries.csv; prior exact TC model and BOM; explicit owner definitions' if fid else 'candidate evidence only',
  'family_observed_at':db_observed_at,
  'annual_orders':len(metrics[pid]['orders']),'annual_market_units':metrics[pid]['units'],'annual_ordered_value_rub':format(D(metrics[pid]['sales_cents'])/100,'.2f'),
  'annual_cpc_spend_rub':format(D(metrics[pid]['cpc_cents'])/100,'.2f')}
 out.append(row)
 if len(p['components'])>1 or int(c['bundle_multiplier'])>1:
  for code,q in p['components'].items():compositionrows.append({'product_id':pid,'offer_id':c['offer_id'],'component_external_code':code,'quantity_per_market_unit':q,
    'component_model':models.get(code,{}).get('title',''),'component_own_current_ozon_card':code in current_offers,
    'source':p['source'],'observed_at':p['at'],'money_allocation':'none; component is not an extra sale'})
 # Explicit multi-parent candidates without a corroborating full BOM stay proposed.
 # They are reviewed as a policy group, never silently promoted from PF to family.
pending_codes=collections.defaultdict(set)
for c in out:
 if c['family_basis']=='parent_group_only_not_final_big_family':
  k=key(c['physical_parent_code'])
  if k[0] and k[1]:pending_codes[k].add(c['physical_parent_code'])
parent_candidates=[];candidate_by_code={}
for k,codes in pending_codes.items():
 if len(codes)<2:continue
 cid='C-'+hashlib.sha256(js(k).encode()).hexdigest()[:12]
 for code in codes:candidate_by_code[code]=cid
 parent_candidates.append({'candidate_family_id':cid,'series':k[0],'functional_role':k[2],'parent_codes':js(sorted(codes)),
  'exact_models':js({code:models[code].get('title') for code in sorted(codes)}),'full_printer_profile':js(k[1]),
  'status':'proposed_not_confirmed_business_boundary','basis':'same explicit TC series + same complete compatibility + same role; no corroborating full BOM',
  'needed':'owner/manufacturer confirmation that resource/chip/OEM variants belong to one management family','source':'prior db_prc_tc_model snapshot; exact physical parent links',
  'observed_at':db_observed_at})
for c in out:
 if c['family_basis']=='parent_group_only_not_final_big_family' and c['physical_parent_code'] in candidate_by_code:
  c['candidate_family_ids']=js([candidate_by_code[c['physical_parent_code']]])
  c['question_group']='Q-PARENT-CANDIDATE'
write('parent_family_candidates.csv',parent_candidates)
write('cards.csv',out);write('components.csv',compositionrows);write('family_boundary_proposals.csv',proposals)
save('family_definitions.json',{fid:{**f,'parent_codes':sorted(f['parent_codes']),'composition_codes':sorted(f['composition_codes']),'related_question_ids':sorted(f['related_question_ids'])} for fid,f in families.items()})
save('map_coverage.json',{'current_cards':len(out),'excluded_archived_cards_technical_count':25956,'unique_product_ids':len({x['product_id'] for x in out}),
 'families_with_corroborated_series':len(families),'proposed_parent_series_candidates':len(parent_candidates),'assigned_cards':sum(bool(x['family_id']) for x in out),'physical_known':sum(bool(x['physical_parent_code']) for x in out),
 'assignment_states':dict(collections.Counter(x['family_basis'] for x in out if not x['family_id'])), 'owner_rule':owner,'no_transitive_union':True,
 'confirmed_annual_profit_available':False,'period':[START,END]})
print(js(read(O/'map_coverage.json')))

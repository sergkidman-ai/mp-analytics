# поток: ozon-family. Offline final map; all writes confined to this audit directory.
from pathlib import Path
import csv,json,hashlib,collections,datetime
O=Path(__file__).parent;S=O/'snapshot';A=O/'agents'
def read(p):return json.loads(p.read_text())
def rows(p):return list(csv.DictReader(p.open(encoding='utf-8-sig',newline='')))
def js(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def csvout(name,data):
 with (O/name).open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
def yes(x):return str(x).lower()=='true'
def main():
 ac=rows(A/'a_cards.csv');bm={x['product_id']:x for x in rows(A/'b_glue_members.csv')}
 availability={}
 for name in ['catalog.json','catalog_archived.json']:
  for c in read(S/name):
   av=c.get('availabilities') or [];states=sorted({x.get('availability','') for x in av})
   availability[str(c['id'])]={'states':states,'sources':[{k:x.get(k) for k in ['sku','source','availability','reasons']} for x in av],
    'has_stock':(c.get('visibility_details') or {}).get('has_stock'),'has_price':(c.get('visibility_details') or {}).get('has_price')}
 contexts=read(A/'a_tc_contexts.json')
 if isinstance(contexts,list):contexts={c['set_code']:c for c in contexts}
 # a_tc_contexts.json may wrap metadata separately.
 if 'contexts' in contexts:contexts=contexts['contexts']
 models={x['external_code']:x for x in read(S/'db_prc_tc_model.json')}
 ms=collections.defaultdict(set)
 for x in read(S/'db_ms_product.json'):
  if not x['archived']:ms[x['external_code']].add(x['ms_id'])
 seeds={};codefamily={};leaf_families=collections.defaultdict(set)
 for code,ctx in sorted(contexts.items()):
  leaves=sorted(ctx['components'])
  if len(leaves)<2:continue
  # Exact codes preserve model/resource/chip distinctions. No graph closure.
  fid='BF-'+hashlib.sha256(js(leaves).encode()).hexdigest()[:12]
  r=seeds.setdefault(fid,{'family_id':fid,'family_kind':'full_composition_context','boundary_status':'composition_confirmed_boundary_proposed',
   'set_parent_codes':[],'component_parent_codes':leaves,'boundary_rule':'exact_full_leaf_code_set; multiplicities retained in components; no shared-component/printer transitive union',
   'source':'snapshot/db_set_cost.json; snapshot/db_prc_tc_model.json','observed_at':[], 'assigned_cards':0,'assigned_current_cards':0,'candidate_cards':0})
  r['set_parent_codes'].append(code);r['observed_at'].extend(p['observed_at'] for p in ctx['proof'])
  codefamily[code]=fid
  for leaf in leaves:leaf_families[leaf].add(fid)
 cards=[];exceptions=[]; comps=[]
 def ex(scope,key,kind,detail,next_check,source,at,classification='требует проверки'):
  exceptions.append({'exception_id':f'EX{len(exceptions)+1:06d}','scope':scope,'entity_id':key,'classification':classification,'exception_type':kind,'detail':detail,'next_check':next_check,'source':source,'observed_at':at})
 for a in ac:
  pid=a['product_id'];b=bm[pid];physical=json.loads(a['physical_components']);confirmed=bool(physical)
  pc=a['identity_code'] if confirmed else '';pg='PG:'+pc if pc else ''
  candidates=sorted(leaf_families.get(pc,set())) if confirmed else []
  fid='';fs='unresolved_physical_mapping'
  if confirmed:
   if pc in codefamily:fid=codefamily[pc];candidates=[fid];fs='confirmed_composition_context'
   elif len(candidates)==1:fid=candidates[0];fs='unique_direct_component_context'
   elif len(candidates)>1:fs='ambiguous_shared_component'
   else:
    fid='PF:'+pc;fs='parent_only_provisional'
    if fid not in seeds:seeds[fid]={'family_id':fid,'family_kind':'parent_only_provisional','boundary_status':'not_a_confirmed_large_family','set_parent_codes':[],'component_parent_codes':[pc],'boundary_rule':'only confirmed parent group; cross-parent boundary not established','source':a['composition_source'],'observed_at':[a['composition_observed_at']],'assigned_cards':0,'assigned_current_cards':0,'candidate_cards':0}
  if fid:seeds[fid]['assigned_cards']+=1;seeds[fid]['assigned_current_cards']+=not yes(a['archived'])
  for cf in candidates:seeds[cf]['candidate_cards']+=1
  model_id=b['model_id']; glue_state='unknown' if not model_id or model_id=='0' else 'api_singleton' if yes(b['api_singleton_confirmed']) else 'confirmed_api_group'
  card={'product_id':pid,'offer_id':a['offer_id'],'ozon_sku':a['sku'],'ozon_sku_aliases':b['sku_aliases'],'name':a['name'],
   'catalog_scope':'archived' if yes(a['archived']) else 'current_nonarchived','is_autoarchived':b['is_autoarchived'],
   'status_name':json.loads(b['status_json']).get('status_name',''),'availability_states':js(availability[pid]['states']),
   'availability_by_source':js(availability[pid]['sources']),'any_source_available':'AVAILABLE' in availability[pid]['states'],
   'any_source_hidden':'HIDDEN' in availability[pid]['states'],'visibility_has_stock':availability[pid]['has_stock'],'visibility_has_price':availability[pid]['has_price'],
   'category_id':b['description_category_id'],'type_id':b['type_id'],
   'article_kind':a['article_kind'],'code_anchor':a['article_anchor'],'specification_parent_code':pc,'warehouse_parent_code':pc if ms[pc] else '',
   'warehouse_link_status':'exact_live_ms_external_code' if pc and ms[pc] else 'no_live_ms_code' if pc else 'unresolved', 'parent_group_id':pg,
   'fulfillment_type':a['product_kind'],'bundle_multiplier':a['multiplier'],'bundle_multiplier_status':a['multiplier_status'],
   'seo_flag':a['seo_flag'],'seo_suffix_candidate':a['seo_suffix_candidate'],
   'large_family_id':fid,'family_status':fs,'candidate_family_ids':js(candidates),'link_status':a['mapping_status'],'composition_status':a['composition_status'],
   'model_id':model_id if model_id!='0' else '','glue_status':glue_state,'ozon_color':a['ozon_color'],'mapping_veto':a['mapping_veto'],
   'card_source':a['source'],'observed_at':a['observed_at'],'attribute_source':a['attrs_source'],'attribute_observed_at':a['attrs_observed_at'],
   'physical_source':a['composition_source'],'physical_observed_at':a['composition_observed_at']}
  cards.append(card)
  if not confirmed:
   ex('product_id',pid,'physical_link_unresolved',f"offer={a['offer_id']}; anchor={a['article_anchor']}; {a['composition_status']}; veto={a['mapping_veto']}",'Read exact full offer specification/assembly mapping in TC/MS; match color, model, role and quantity; retain anchor independently',a['source']+';'+a['attrs_source'],a['observed_at'])
  elif fs=='ambiguous_shared_component':
   ex('product_id',pid,'shared_component_family_boundary',js(candidates),'Owner chooses one disjoint reporting boundary or keep contextual families nonadditive; no transitive merge',a['composition_source'],a['composition_observed_at'],'обоснованное исключение')
  if pc and not ms[pc]:
   ex('product_id',pid,'no_live_ms_parent_record',pc,'Find exact externalCode in MS including archive and confirm supplier/warehouse item; TC composition alone is not a live stock record','snapshot/db_ms_product.json',read(S/'db_manifest.json')['started_at'])
  if int(a['multiplier'])>1 or (confirmed and len(physical)>1):
   for code,qty in sorted(physical.items()):
    comps.append({'product_id':pid,'offer_id':a['offer_id'],'fulfillment_type':a['product_kind'],'component_external_code':code,'quantity_per_card':qty,
     'component_parent_group':'PG:'+code,'component_ms_ids':js(sorted(ms[code])),'component_ms_link_status':'exact_live_ms_external_code' if ms[code] else 'not_found_live_ms',
     'component_model':models.get(code,{}).get('title',''),'component_type':models.get(code,{}).get('consumable_type',''),
     'status':a['composition_status'],'source':a['composition_source'],'observed_at':a['composition_observed_at'],'card_source':a['source'],'card_observed_at':a['observed_at']})
 for b in rows(A/'b_exceptions.csv'):
  ex(b['scope'],b['key'],b['exception_type'],b['detail'],
   'Check current Ozon cabinet group/export and category rule; for count mismatch resolve scope/count and ninth member; no live changes',b['evidence_source'] or 'agents/b_glue_groups.csv',read(S/'attribute_coverage.json')['completed_at'])
 ex('source','db','shipment_execution_evidence_unavailable','ms_shipment_bom=0; current assessments=0; raw posting→demand-position exact join=0','Use saved verified order evidence for examples; targeted read of MS order/demand if specific SEO needs execution proof','snapshot/fulfillment_evidence.json',read(S/'fulfillment_evidence.json')['observed_at'])
 ex('source','tc_color','normalized_color_missing','All TC normalized color NULL and raw ink_colors empty; prc_tc_link 0 rows','Read exact TC specification for ambiguous color/series; do not infer physical composition from black code prefix','snapshot/db_prc_tc_model.json; snapshot/db_prc_tc_link.json',read(S/'db_manifest.json')['started_at'])
 for b in bm.values():
  if not b['schema_source']:
   ex('product_id',b['product_id'],'category_schema_unavailable',f"category={b['description_category_id']};type={b['type_id']}",'Read correct archived category/type definition from Ozon cabinet or exact category API after resolving type=0; no content verdict',b['evidence_source'],b['observed_at'])
 ex('external_code','5129','tc_role_conflict','TC labels 5129 as cartridge within 5133 photodrum set; 5133 is NOT toner+drum','Verify 5129 original manufacturer/MS description before changing catalog role; BOM quantity remains source-confirmed','snapshot/db_prc_tc_model.json; snapshot/db_set_cost.json',read(S/'db_manifest.json')['started_at'])
 ex('source','annual_finance','incomplete_year_economics','No finance Nov-Dec2025; accrual through03Nov2026; 231 NULL cogs in 8149 monthly rows','Reconcile financial source/COGS coverage in separate annual phase; no zero-filling','snapshot/db_year_monthly_coverage.json',read(S/'db_year_monthly_coverage.json')['observed_at'])
 families=[]
 for f in seeds.values():
  f['observed_at']=js(sorted(set(x for x in f['observed_at'] if x)))
  f['set_parent_codes']=js(sorted(f['set_parent_codes']));f['component_parent_codes']=js(f['component_parent_codes']);families.append(f)
 glue=[]
 for c in cards:
  b=bm[c['product_id']]
  glue.append({'model_id':c['model_id'],'product_id':c['product_id'],'offer_id':c['offer_id'],'glue_status':c['glue_status'],
   'reported_count':b['reported_count'],'observed_group_products':b['observed_group_products'],'count_status':b['count_state'],
   'catalog_scope':c['catalog_scope'],'card_kind':c['fulfillment_type'],'seo_flag':c['seo_flag'],'parent_group_id':c['parent_group_id'],
   'large_family_id':c['large_family_id'],'candidate_family_ids':c['candidate_family_ids'],'source':b['evidence_source'],'observed_at':b['observed_at'],
   'attributes_source':b['attributes_source'],'attribute_observed_at':b['attributes_observed_at'],'storefront_verified':'false','historical_membership_verified':'false'})
 csvout('cards.csv',cards);csvout('components.csv',comps);csvout('families.csv',families);csvout('glue_members.csv',glue);csvout('exceptions.csv',exceptions)
 stats={}
 for scope,subset in [('all',cards),('current',[c for c in cards if c['catalog_scope']=='current_nonarchived']),('archived',[c for c in cards if c['catalog_scope']=='archived'])]:
  stats[scope]={'cards':len(subset),'parent_groups':len({c['parent_group_id'] for c in subset if c['parent_group_id']}),'code_anchors':len({c['code_anchor'] for c in subset if c['code_anchor']}),
   'physical_link_known':sum(bool(c['parent_group_id']) for c in subset),'physical_link_unknown':sum(not c['parent_group_id'] for c in subset),
   'types':dict(collections.Counter(c['fulfillment_type'] for c in subset)), 'article_kinds':dict(collections.Counter(c['article_kind'] for c in subset)),
   'family_statuses':dict(collections.Counter(c['family_status'] for c in subset)),'family_contexts_assigned':len({c['large_family_id'] for c in subset if c['large_family_id'].startswith('BF-')}),
   'glue_statuses':dict(collections.Counter(c['glue_status'] for c in subset)), 'status_names':dict(collections.Counter(c['status_name'] for c in subset)),
   'availability_state_combinations':dict(collections.Counter(c['availability_states'] for c in subset)),
   'unknown_physical_or_family_or_glue':sum(not c['parent_group_id'] or c['family_status'] in ('ambiguous_shared_component','parent_only_provisional') or c['glue_status']=='unknown' for c in subset)}
 stats['family_context_rows']=sum(f['family_kind']=='full_composition_context' for f in families);stats['provisional_family_rows']=sum(f['family_kind']=='parent_only_provisional' for f in families)
 stats['exception_rows']=len(exceptions);stats['component_rows']=len(comps);stats['built_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 (O/'map_summary.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2))
 print(json.dumps(stats,ensure_ascii=False))
if __name__=='__main__':main()

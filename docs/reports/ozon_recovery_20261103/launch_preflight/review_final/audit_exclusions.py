# поток: mkt
"""Offline audit of the exact prior manifest and family-only exclusions."""
import pathlib,json,csv,collections,decimal,hashlib,datetime as dt
O=pathlib.Path(__file__).parent;PRE=O.parent;ROOT=PRE.parent;D=decimal.Decimal

def load(p):return json.loads(p.read_text())
def read(p):
 with p.open() as f:return list(csv.DictReader(f))
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def write(n,rs):
 with (O/n).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
old=PRE/'launch_manifest.json';assert hashlib.sha256(old.read_bytes()).hexdigest()=='46c202d8563ae0165bdca6c0e2516424bc8409007ba0778c7b13cf6cd833bdd5'
excluded=read(PRE/'excluded_skus.csv');allr=load(PRE/'eligibility_final.json');fa={r['model_id']:r for r in load(PRE/'family_audit.json')}
items={};alias={};sources={};snap={}
for p in sorted((PRE/'raw').glob('*_info_*.json')):
 z=load(p)
 if z['http']!=200:continue
 for x in z['body'].get('items',[]):
  pid=str(x['id']);items[pid]=x;sources[pid]=str(p.relative_to(ROOT));snap[pid]=z['retrieved_at_utc']
  for s in [x.get('sku')]+[a.get('sku') for a in x.get('sources',[])]:
   if s:alias[str(s)]=pid
active=set(load(PRE/'bounded_universe.json')['active_cpc_skus'])
active_by_model=collections.defaultdict(set)
for s in active:
 if s in alias:active_by_model[str(items[alias[s]]['model_info']['model_id'])].add(s)
def owncheck(r):
 it=items.get(r['product_id']);reasons=[]
 if not it:return ['OWN_METADATA_MISSING']
 if it.get('is_archived') or it.get('is_autoarchived'):reasons.append('OWN_ARCHIVED')
 try:price=D(str(it.get('price')))
 except Exception:price=None
 if price is None or not price.is_finite() or price<=0:reasons.append('OWN_PRICE_INVALID')
 if it.get('currency_code')!='RUB':reasons.append('OWN_CURRENCY_NOT_RUB')
 if not it.get('stocks',{}).get('has_stock'):reasons.append('OWN_NO_STOCK')
 if not it.get('visibility_details',{}).get('has_price'):reasons.append('OWN_PRICE_NOT_VISIBLE')
 if not any(a.get('availability')=='AVAILABLE' for a in it.get('availabilities',[])):reasons.append('OWN_NOT_AVAILABLE')
 if r['sku'] in active:reasons.append('OWN_ACTIVE_CPC')
 if str(it.get('model_info',{}).get('model_id'))!=r['model_id']:reasons.append('OWN_MAPPING_MISMATCH')
 if any(str(e.get('level','')).upper()=='ERROR' for e in it.get('errors',[])):reasons.append('OWN_CARD_ERROR')
 return reasons
priority=['FAMILY_ALREADY_IN_RUNNING_CPC','FAMILY_COMPOSITION_INCOMPLETE','FAMILY_MEMBER_FAILED_CHECK']
audit=[]
for r in excluded:
 rr=r['reasons'].split(';');direct=owncheck(r);primary=next((v for v in priority if v in rr),rr[0]);other=[v for v in rr if v!=primary]
 audit.append({'sku':r['sku'],'stream':r['stream'],'offer_id':r['offer_id'],'model_id':r['model_id'],'original_primary_reason':primary,'original_additional_reasons':';'.join(other),'own_direct_failures':';'.join(direct),'own_direct_failed':bool(direct),'excluded_only_due_to_family':not direct,'source':str((PRE/'excluded_skus.csv').relative_to(ROOT)),'own_metadata_source':sources.get(r['product_id']),'evidence_kind':'family-context extrapolation; no individual failure' if not direct else 'individual failure'})
write('old_exclusion_per_sku.csv',audit)
summary=[]
for stream in ['new','bundles','old']:
 for reason in priority:
  rr=[r for r in audit if r['stream']==stream and r['original_primary_reason']==reason]
  summary.append({'stream':stream,'primary_reason':reason,'sku_direct_failures':sum(r['own_direct_failed'] for r in rr),'additional_family_only_skus':sum(r['excluded_only_due_to_family'] for r in rr),'total_excluded':len(rr),'families_affected':len({r['model_id'] for r in rr}),'counting':'primary reason precedence ACTIVE_FAMILY > INCOMPLETE_FAMILY > FAILED_NEIGHBOR; family totals not additive across reasons/streams'})
write('old_exclusions_by_stream_reason.csv',summary)
extras=[]
for stream in ['new','bundles','old']:
 for reason in priority:
  rr=[r for r in audit if r['stream']==stream and reason in r['original_additional_reasons'].split(';')]
  extras.append({'stream':stream,'additional_reason':reason,'sku_count':len(rr),'families_affected':len({r['model_id'] for r in rr}),'not_additive_to_primary':True})
write('old_additional_reasons.csv',extras)
# Reusable forensic examples: three bundles per main family rule, plus failed-neighbor secondary rule.
examples=[]
for reason in priority:
 candidates=[r for r in excluded if r['stream']=='bundles' and reason in r['reasons'].split(';') and (reason=='FAMILY_MEMBER_FAILED_CHECK' or next(x for x in audit if x['sku']==r['sku'])['original_primary_reason']==reason)]
 chosen=[];seen=set()
 for r in sorted(candidates,key=lambda r:(r['model_id'],r['sku'])):
  if r['model_id'] in seen:continue
  chosen.append(r);seen.add(r['model_id'])
  if len(chosen)==3:break
 for r in chosen:
  pid=r['product_id'];it=items[pid];family=fa[r['model_id']]
  neighbor=[]
  if reason=='FAMILY_ALREADY_IN_RUNNING_CPC':
   for s in sorted(active_by_model[r['model_id']])[:3]:
    np=alias[s];n=items[np];neighbor.append({'sku':s,'product_id':np,'offer_id':n['offer_id'],'model_info':n['model_info'],'source':sources[np],'active_campaign_sources':[str(fp.relative_to(ROOT)) for fp in (PRE/'raw').glob('active_products_*.json') if any(str(v['sku'])==s for v in load(fp).get('products',[]))],'observation':'SKU in RUNNING CPC; not proof of treatment/control'})
  if reason=='FAMILY_MEMBER_FAILED_CHECK':
   for np,errs in family['failed_member_details'].items():
    n=items[np];neighbor.append({'sku':str(n['sku']),'product_id':np,'offer_id':n['offer_id'],'stocks':n.get('stocks'),'availabilities':n.get('availabilities'),'direct_failures':errs,'source':sources[np]})
  evidence={'sku':r['sku'],'offer_id':r['offer_id'],'model_id':r['model_id'],'rule':reason,'own_http':200,'own_response_fields':{k:it.get(k) for k in ['id','sku','offer_id','price','currency_code','stocks','availabilities','model_info','is_archived','errors']},'own_checked_at_utc':snap[pid],'own_source':sources[pid],'known_family_products':family['known_products'],'reported_family_counts':family['reported_counts'],'neighbor_evidence':neighbor,'classification':'own SKU confirmed available; active sibling / unqueried siblings / unavailable neighbor is context, not own unavailability','original_rule_action':'exclude every recovered SKU with same model_id','review_action':'remove blanket individual exclusion; keep assignment and contamination checks as separate experimental condition'}
  name=f'example_{reason}_{r["sku"]}.json';save('raw/'+name,evidence)
  examples.append({'reason':reason,'sku':r['sku'],'offer_id':r['offer_id'],'model_id':r['model_id'],'own_stock_has_stock':it['stocks']['has_stock'],'own_available':any(x['availability']=='AVAILABLE' for x in it['availabilities']),'own_price_rub':it['price'],'known_family_products':family['known_products'],'api_family_count':json.dumps(family['reported_counts']),'evidence_file':'raw/'+name,'original_rule':'whole-family exclusion','reviewed_result':'own SKU remains individually eligible; experiment assignment not inferred'})
write('bundles_examples.csv',examples)
# Audit packing: campaigns are delivery shards, not randomized treatment/control arms.
m=load(old);alloc=collections.defaultdict(set)
for c in m['campaigns']:
 for r in c['sku_bids']:alloc[(c['stream'],r['model_id'])].add(c['key'])
oldsplit=[{'stream':s,'model_id':mid,'campaigns':sorted(v)} for (s,mid),v in alloc.items() if s=='old' and len(v)>1]
assert not oldsplit
save('old_campaign_packing_audit.json',{'old_sizes':[c['sku_count'] for c in m['campaigns'] if c['stream']=='old'],'old_model_blocks_split_between_old_shards':oldsplit,'all_shards_experiment_assignment':'no treatment/control assignments in source manifest; shard number is NOT an arm','four_campaign_reason':'prior implementer operational <=500 SKU cap; not a recovered experiment division and not verified vendor total limit','cross_stream_model_count':len(m['cross_stream_current_families'])})
# Identified own eligibility universe for corrected proposal. No family count or sibling stock heuristic is used.
corrected=[]
for r in allr:
 failures=owncheck(r)
 corrected.append({'sku':r['sku'],'stream':r['stream'],'product_id':r['product_id'],'offer_id':r['offer_id'],'name':r['name'],'model_id':r['model_id'],'own_failure_reasons':failures,'individually_eligible':not failures,'old_passed':r['passed_all_checks'],'removed_blanket_family_reasons':r['reasons'] if not failures else [],'existing_floor':r.get('floor'),'assignment':'unresolved; no Nov-2026 family arm mapping in recovered source','metadata_source':r['metadata_source']})
save('corrected_eligibility.json',corrected)
save('assignment_source_audit.json',{'recovered_manifest_has_treatment_control':False,'recovered_2953_has_treatment_control':False,'related_files_searched':['launch_manifest.json','revised_prepared_composition_2953.csv','docs/experiments/ozon_experiments.json','docs/experiments/ozon_hypotheses.json','docs/experiments/ozon_observational_studies.json'],'conclusion':'no confirmed Nov first-wave family arm assignment found; old August/September experiments are not imported by overlap alone','no_existing_assignment_changed':True,'no_randomization_performed':True,'family_unit_preserved':'all old and new shards share a family ledger; no shard ID used as T/C assignment','pending_user_clarification':True})
print('Direct vs family-only by stream',[(s,sum(r['own_direct_failed'] for r in audit if r['stream']==s),sum(r['excluded_only_due_to_family'] for r in audit if r['stream']==s),len({r['model_id'] for r in audit if r['stream']==s})) for s in ['new','bundles','old']])
for r in summary:
 if r['total_excluded']:print(r)
print('Individual eligible after review',dict(collections.Counter(r['stream'] for r in corrected if r['individually_eligible'])))
print('Missing floors only',sum(r['individually_eligible'] and not r['existing_floor'] for r in corrected))

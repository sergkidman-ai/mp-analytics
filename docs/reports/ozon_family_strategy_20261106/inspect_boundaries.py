# поток: ozon-family-strategy. Corroborated boundaries, never graph closure.
from common import *
import collections,re
cat,cards,alias=current();byoffer={x['offer_id']:x for x in cards.values()}
models={x['external_code']:x for x in read(S/'db_prc_tc_model.json')}
contexts={x['set_code']:x for x in read(AUD/'agents/a_tc_contexts.json') if x['set_code'] in byoffer}
def printers(code):
 return tuple(sorted({(str(x.get('brand','')).strip().casefold(),str(x.get('title','')).strip().casefold()) for x in models.get(code,{}).get('printer_models') or []}))
def role(code):
 t=models.get(code,{}).get('consumable_type','')
 return 'cartridge' if t in ['Картридж','Тонер-картридж'] else 'drum' if t=='Фотобарабан' else t
evidence=[];cores={}
for code,ctx in contexts.items():
 leaves=sorted(ctx['components']);m=models.get(code,{});roles={role(c) for c in leaves}
 series={models.get(c,{}).get('united_title') for c in leaves};ps={printers(c) for c in leaves}
 eligible=len(leaves)>1 and len(roles)==1 and roles<={'cartridge','Чернила','drum'} and len(ps)==1 and bool(next(iter(ps))) and len(series)==1 and bool(next(iter(series))) and printers(code)==next(iter(ps))
 if role(code)=='Комплект фотобарабанов' and roles!={'drum'}:eligible=False
 evidence.append({'set_code':code,'set_model':m.get('title'),'set_type':m.get('consumable_type'),'components':js(ctx['components']),
 'component_roles':js(sorted(roles)),'component_series':js(sorted(series,key=str)),'same_full_printer_profile':len(ps)==1,'parent_matches_full_profile':printers(code) in ps and len(ps)==1,
 'corroborated_series_core':eligible,'source':'prior set_cost BOM + prc_tc_model exact leaf codes, united_title, full printer_models; not name alone',
 'source_date':min(x['observed_at'] for x in ctx['proof'])})
 if eligible:cores[code]=ctx
extensions=[]
for code,ctx in contexts.items():
 if models[code].get('consumable_type')!='Фотобарабан + картридж':continue
 leaves=set(ctx['components']);toner={c for c in leaves if role(c)=='cartridge'};drums={c for c in leaves if role(c)=='drum'}
 for core,base in cores.items():
  if set(base['components'])!=toner or toner|drums!=leaves or not drums:continue
  cp=set(printers(core));mp=set(printers(code));drumprofiles={d:set(printers(d)) for d in drums}
  corroborated=bool(cp and cp==mp and all(dp and dp<=cp for dp in drumprofiles.values()))
  extensions.append({'core_set':core,'mixed_set':code,'extra_drum_codes':js(sorted(drums)),
   'complete_core_contained':True,'mixed_and_core_full_printers_equal':cp==mp,'each_drum_profile_nonempty_subset':all(dp and dp<=cp for dp in drumprofiles.values()),
   'corroborated':corroborated,'boundary_status':'pending_owner_rule','source':'exact BOM containment + role + full parent compatibility + drum compatibility subset'})
write('composition_boundaries.csv',evidence)
write('mixed_extensions.csv',extensions)
save('boundary_summary.json',{'current_set_codes':len(contexts),'corroborated_series_cores':len(cores),'mixed_extensions':len(extensions),'corroborated_mixed_extensions':sum(x['corroborated'] for x in extensions),
 'focus':[x for x in extensions if x['mixed_set'] in ['6629','6642']], 'no_automatic_union':True})
print(js(read(O/'boundary_summary.json')))

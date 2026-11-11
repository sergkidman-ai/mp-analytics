# поток: ozon-family-strategy; reuse stored evidence, preserve unresolved assemblies.
from common import *
import re,collections
cat,cards,alias=current();byoffer={c['offer_id']:c for c in cards.values()};models={x['external_code']:x for x in read(S/'db_prc_tc_model.json')}
attrs={}
for p in sorted(S.glob('attrs_*.json')):
 z=read(p)
 for x in z['body'].get('result',[]):
  pid=str(x['id'])
  if pid not in cat:continue
  values={a['id']:[str(v.get('value','')) for v in a.get('values',[])] for a in x.get('attributes',[]) if a['id'] in [9602,8513]}
  attrs[pid]={'colors':values.get(9602,[]),'quantity':values.get(8513,[]),'source':str(p.relative_to(O.parent)),'at':z['observed_at']}
def norm(s):return re.sub(r'[^a-zа-яё0-9]','',str(s).casefold())
output=[]
for pid,c in cards.items():
 if c['specification_parent_code']:continue
 code=c['code_anchor'];m=models.get(code,{});parent=byoffer.get(code);a=attrs.get(pid,{})
 pa=attrs.get(parent['product_id'],{}) if parent else {}
 compatible=[p for p in m.get('printer_models') or [] if len(norm(p.get('title')))>3 and norm(p.get('title')) in norm(c['name'])]
 ordinary_types={'Картридж','Тонер-картридж','Фотобарабан','Тонер','Чернила','Лента для принтера','Блок проявки','Бункер отработанного тонера','Печатающая головка'}
 reasons=[]
 if c['article_kind']!='seo_numeric_child':reasons.append('not_ordinary_numeric_seo')
 if m.get('consumable_type') not in ordinary_types:reasons.append('tc_role_not_single')
 if not parent or parent['specification_parent_code']!=code or parent['fulfillment_type']!='single':reasons.append('parent_single_physical_link_not_confirmed')
 if a.get('quantity')!=['1'] or pa.get('quantity')!=['1']:reasons.append('not_explicit_one_item_both_cards')
 if not a.get('colors') or set(a.get('colors',[]))!=set(pa.get('colors',[])):reasons.append('parent_child_color_not_equal')
 if not compatible:reasons.append('no_explicit_parent_compatible_printer_in_child_title')
 if re.search(r'\b(комплект|набор|картриджи|фотобарабаны)\b',c['name'],re.I):reasons.append('assembly_words_in_child_title')
 if re.search(r'фотобарабан|drum',c['name'],re.I) and m.get('consumable_type')!='Фотобарабан':reasons.append('role_conflict')
 output.append({'product_id':pid,'offer_id':c['offer_id'],'code_anchor':code,'tc_type':m.get('consumable_type',''),'tc_model':m.get('title',''),
 'resolved':not reasons,'physical_parent_code':code if not reasons else '',
 'rule':'owner_ordinary_single_rule_corroborated_parent_child_qty_color_and_exact_printer' if not reasons else '',
 'reasons':js(reasons),'matching_printers':js(compatible),'child_colors':js(a.get('colors',[])),'parent_colors':js(pa.get('colors',[])),
 'child_quantity':js(a.get('quantity',[])),'parent_quantity':js(pa.get('quantity',[])),
 'evidence_source':a.get('source','')+';'+pa.get('source','')+';ozon_family_audit_20261106/snapshot/db_prc_tc_model.json;owner ordinary SEO definition',
 'observed_at':a.get('at','')})
write('physical_resolution.csv',output)
save('physical_resolution_summary.json',{'previous_current_unknown':len(output),'resolved':sum(x['resolved'] for x in output),'remaining':sum(not x['resolved'] for x in output),
 'rule_is_limited_to_ordinary_single':True,'no_set_composition_inferred':True})
print(js(read(O/'physical_resolution_summary.json')))

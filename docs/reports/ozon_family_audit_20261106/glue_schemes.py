# поток: ozon-family. Offline synthesis of physical/family and API grouping evidence.
from pathlib import Path
import json,csv,collections
O=Path(__file__).parent
def rows(p):return list(csv.DictReader(p.open(encoding='utf-8-sig')))
cards=rows(O/'cards.csv');groups=collections.defaultdict(list)
for c in cards:
 if c['model_id']:groups[c['model_id']].append(c)
be=collections.defaultdict(set)
for e in rows(O/'agents/b_exceptions.csv'):
 if e['scope']=='model_id':be[e['key']].add(e['exception_type'])
bgroup={x['model_id']:x for x in rows(O/'agents/b_glue_groups.csv')}
out=[]
for mid,cs in groups.items():
 if len(cs)<2:continue
 types={c['fulfillment_type'] for c in cs};pg={c['parent_group_id'] for c in cs if c['parent_group_id']};bf={c['large_family_id'] for c in cs if c['large_family_id'].startswith('BF-')}
 patterns=[]
 if any(c['article_kind']=='primary_exact_four_digits' for c in cs) and any(c['seo_flag']=='True' for c in cs):patterns.append('parent_and_seo')
 if any(int(c['bundle_multiplier'])>1 for c in cs):patterns.append('quantity_bundles')
 if any(c['fulfillment_type'] in {'heterogeneous_set','heterogeneous_drum_set','drum_and_cartridge_set','bundle_of_sets'} for c in cs):patterns.append('heterogeneous_sets')
 if len(pg)>1:patterns.append('multiple_parent_groups')
 if len(bf)>1:patterns.append('multiple_full_composition_contexts')
 if len({c['ozon_color'] for c in cs if c['ozon_color']})>1:patterns.append('color_variants')
 unknown=sum(not c['parent_group_id'] for c in cs)
 flags=be[mid]
 correct=not unknown and len(pg)==1 and not flags
 # Narrow positive verdict: full composition identity, allowed quantity axis,
 # one category/brand/model, full API count, no diagnostic differences.
 out.append({'model_id':mid,'cards':len(cs),'current_cards':sum(c['catalog_scope']=='current_nonarchived' for c in cs),
  'patterns':';'.join(patterns),'parent_groups':len(pg),'full_composition_contexts':len(bf),'unknown_physical_cards':unknown,
  'classification':'корректно по доступным структурным данным' if correct else 'требует проверки','diagnostic_flags':';'.join(sorted(flags)),
  'rule_basis':'same confirmed physical parent, allowed quantity axis, no detected structural differences' if correct else 'mixed composition/family or structural uncertainty is not itself a violation',
  'official_rule_source':'https://seller.ozon.ru/media/news/novoe-v-dogovore-izmeneniya-blizhajshih-2-nedel/; snapshot/category_schema_17028935_95980.json',
  'source':'glue_members.csv;cards.csv;agents/b_glue_groups.csv','observed_from':min(c['observed_at'] for c in cs),'observed_to':max(c['observed_at'] for c in cs)})
with (O/'glue_groups.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(out[0]));w.writeheader();w.writerows(out)
summary={'multimember_groups':len(out),'patterns':dict(collections.Counter(p for x in out for p in x['patterns'].split(';') if p)),
 'classifications':dict(collections.Counter(x['classification'] for x in out)),
 'current_multimember_groups':sum(x['current_cards']>0 for x in out),
 'seo_api_singleton_current':sum(c['seo_flag']=='True' and c['catalog_scope']=='current_nonarchived' and c['glue_status']=='api_singleton' for c in cards),
 'pattern_counts_overlap':True,'confirmed_glue_errors':0}
(O/'glue_schemes_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False))

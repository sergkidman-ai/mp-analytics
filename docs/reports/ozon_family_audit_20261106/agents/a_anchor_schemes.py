# поток: ozon-family; offline anchor schemes, CSV outputs remain immutable.
import csv,json
from collections import Counter,defaultdict
from pathlib import Path
P=Path(__file__).parent
S=P.parent/'snapshot'
cards=list(csv.DictReader((P/'a_cards.csv').open()))
models={r['external_code']:r for r in json.loads((S/'db_prc_tc_model.json').read_text())}
contexts=json.loads((P/'a_tc_contexts.json').read_text())
byoffer={r['offer_id']:r for r in cards}
byctx={r['set_code']:r for r in contexts}
seo=[r for r in cards if r['article_kind']=='seo_numeric_child' and r['archived']=='False']
types=Counter(models.get(r['article_anchor'],{}).get('consumable_type','UNKNOWN') for r in seo)
schemes=defaultdict(list)
for r in seo:
    t=models.get(r['article_anchor'],{}).get('consumable_type','UNKNOWN')
    group='mixed_toner_drum_set_anchor' if t=='Фотобарабан + картридж' else 'separate_drum_anchor' if t=='Фотобарабан' else 'separate_cartridge_anchor' if t in {'Картридж','Тонер-картридж'} else 'separate_refill_toner_anchor' if t=='Тонер' else 'other_set_anchor' if 'комплект' in t.lower() else 'other_anchor'
    schemes[group].append(r)

def example(row):
    code=row['article_anchor'];ctx=byctx.get(code)
    return {'offer_id':row['offer_id'],'name':row['name'],'article_anchor':code,
            'parent_name':byoffer.get(code,{}).get('name'),
            'parent_tc_type':models.get(code,{}).get('consumable_type'),
            'parent_tc_model':models.get(code,{}).get('title'),
            'mapping_status':row['mapping_status'],'physical_components_emitted':json.loads(row['physical_components']),
            'anchor_bom_only':ctx['components'] if ctx else None,
            'bom_status':'Exact parent BOM does not prove child physical composition' if ctx else 'No exact parent BOM',
            'mapping_veto':json.loads(row['mapping_veto']),
            'source':row['source'],'observed_at':row['observed_at'],
            'attrs_source':row['attrs_source'],'attrs_observed_at':row['attrs_observed_at']}

# Colour-family scope uses an explicitly declared multi-colour set type/count.
# Missing TC per-component colours prevent treating the scope as fully verified.
color_contexts=[]
for c in contexts:
    m=models.get(c['set_code'],{})
    if m.get('consumable_type') in {'Комплект картриджей','Комплект чернил','Комплект фотобарабанов'} and int(m.get('raw',{}).get('colors_count') or 0)>=2 and len(c['components'])>=2:
        color_contexts.append(c)
setcodes={c['set_code'] for c in color_contexts}
leaf_owners=defaultdict(set)
for c in color_contexts:
    for leaf in c['components']:leaf_owners[leaf].add(c['set_code'])
covered=[]
categories=Counter();physical_statuses=Counter();shared_children=0
for r in seo:
    a=r['article_anchor'];parent=byoffer.get(a,{})
    if a not in setcodes and a not in leaf_owners:continue
    color=parent.get('ozon_color','')
    category='set_code' if a in setcodes else 'K_component_parent' if color=='K' else 'nonK_component_parent' if color in {'C','M','Y'} else 'unknown_or_multi_parent_color'
    categories[category]+=1;physical_statuses[r['composition_status']]+=1
    if len(leaf_owners.get(a,[]))>1:shared_children+=1
    covered.append((r,category))
counts={k:{'children':len(v),'unique_article_anchors':len({r['article_anchor'] for r in v}),
           'mapping_statuses':dict(Counter(r['mapping_status'] for r in v))} for k,v in schemes.items()}
result={'scope':'oz_acc1 current numeric SEO only; ALL+ARCHIVED shared catalog used for parent evidence',
        'current_numeric_seo':len(seo),'exact_bom_context_count':len(contexts),
        'bom_count_note':'Shared snapshot contains992 set_cost rows, not994; no API or extra snapshot used.',
        'anchor_tc_type_counts':dict(types),'anchor_scheme_counts':counts,
        'mixed_set_seo_examples':[example(r) for r in schemes['mixed_toner_drum_set_anchor'][:2]],
        'mixed_set_seo_physical_proof':'All four are article anchors to a mixed-set parent; no child physical BOM/specification was verified, so physical mapping remains unknown.',
        'color_scope':{'status':'candidate_colour_contexts_from_explicit_set_type_and_raw_colors_count_not_verified_complete_colour_spec',
                       'bom_contexts':len(color_contexts),'covered_unique_current_numeric_children':len(covered),
                       'disjoint_anchor_categories':dict(categories),'physical_statuses':dict(physical_statuses),
                       'children_on_leaf_shared_by_multiple_contexts':shared_children,
                       'rule':'set_code takes precedence; K/nonK uses exact component-parent Ozon attribute9602; shared leaves do not union contexts',
                       'examples_by_category':{cat:example(next(r for r,c in covered if c==cat)) for cat in categories}},
        'confidence':'Article scheme counts only. Mixed/colour SEO physical composition is not inherited from anchor.'}
(P/'a_anchor_schemes.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
lines=[
f"Текущих numeric SEO:{len(seo)}; точных BOM в общем snapshot:{len(contexts)} (не994).",
f"Якорь одиночного картриджа/тонер-картриджа:{counts['separate_cartridge_anchor']['children']}; фотобарабана:{counts['separate_drum_anchor']['children']}; заправочного тонера:{counts['separate_refill_toner_anchor']['children']}.",
f"Якорь отдельного смешанного набора «Фотобарабан + картридж»:{counts['mixed_toner_drum_set_anchor']['children']} SEO на{counts['mixed_toner_drum_set_anchor']['unique_article_anchors']} якорях.",
"Два реальных примера mixed-set SEO сохранены в a_anchor_schemes.json; это доказанная артикульная привязка, физсостав дочерних не подтверждён.",
f"Остальные якоря наборов:{counts['other_set_anchor']['children']}; прочие:{counts['other_anchor']['children']}.",
f"Цветовой scope:{len(color_contexts)} BOM-контекстов по явному типу набора + raw.colors_count≥2; это candidate полного цветового семейства.",
f"В scope найдено{len(covered)} уникальных текущих numeric children: под K-компонентом{categories['K_component_parent']}, под неK{categories['nonK_component_parent']}, под кодом набора{categories['set_code']}, цвет родителя неизвестен/смешан{categories['unknown_or_multi_parent_color']}.",
f"У{shared_children} children якорный leaf участвует в нескольких разных BOM; контексты не объединены.",
"6642 назван generic toner+drum: TN/DR встречаются и у Konica Minolta, принадлежность Brother не выводится из букв модели.",
"00331 M при Samsung ML1520 — archived_color_attribute_conflict; это конфликт атрибута источника, не подтверждённая цветная SEO."
]
(P/'a_anchor_schemes.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({'numeric_seo':len(seo),'mixed_set_seo':len(schemes['mixed_toner_drum_set_anchor']),'colour_children':len(covered),'categories':dict(categories),'report_lines':len(lines)},ensure_ascii=False))

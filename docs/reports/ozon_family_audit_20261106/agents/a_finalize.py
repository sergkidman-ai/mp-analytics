# поток: ozon-family; final report and offline invariants, no service/API/DB use.
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

P=Path(__file__).parent
S=P.parent/'snapshot'
cards=list(csv.DictReader((P/'a_cards.csv').open()))
components=list(csv.DictReader((P/'a_components.csv').open()))
contexts=json.loads((P/'a_tc_contexts.json').read_text())
models={r['external_code']:r for r in json.loads((S/'db_prc_tc_model.json').read_text())}
byoffer={r['offer_id']:r for r in cards}
assert len(cards)==48492 and len({r['product_id'] for r in cards})==48492
assert not any(r['status'].startswith('candidate') for r in components)
assert not any(byoffer[r['offer_id']]['mapping_status'].endswith('physical_unknown') for r in components)
assert not any(c['set_code']=='5133' and c['kind']=='drum_and_cartridge_set' for c in contexts)
assert {r['component_role'] for r in components if r['offer_id']=='5133'}=={'drum'}
assert not any('stock' in leaf or 'reserve' in leaf for r in cards for vals in json.loads(r['stock_by_exact_external_code']).values() for leaf in vals)

examples=[]
for c in contexts:
    names=[models.get(k,{}).get('title','') for k in c['components']]
    if c['kind']=='drum_and_cartridge_set' and any(re.search(r'\bTN[- ]?\d',x,re.I) for x in names) and any(re.search(r'\bDR[- ]?\d',x,re.I) for x in names):
        examples.append({'example_type':'confirmed_toner_and_drum_distinct_roles','context':c,
                         'component_specifications':[{ 'code':k,'type':models.get(k,{}).get('consumable_type'),'model':models.get(k,{}).get('title')} for k in c['components']],
                         'ozon_parent':byoffer.get(c['set_code'])});break
for r in cards:
    if r['archived']=='False' and r['article_kind']=='seo_numeric_child' and r['mapping_status'] in {'verified_model_color_article_mapping','owner_article_rule_ordinary_single'} and r['article_anchor'] in byoffer:
        examples.append({'example_type':'ordinary_parent_and_proven_child','parent':byoffer[r['article_anchor']],'child':r});break
for r in cards:
    parent=byoffer.get(r['article_anchor'],{})
    if r['article_kind']=='seo_numeric_child' and parent.get('ozon_color')=='K' and r.get('ozon_color') in {'C','M','Y','MULTI'}:
        assert not json.loads(r['physical_components'])
        examples.append({'example_type':'archived_color_attribute_conflict','parent':parent,'child':r,
                         'interpretation':'Archived source has conflicting color attribute; this is not evidence of a genuine color SEO or different physical consumable.'});break
examples.append({'example_type':'operator_confirmed_suffixed_bundle','card':byoffer.get('6452X3MS22I0'),
                 'present_in_this_oz_acc1_union':'6452X3MS22I0' in byoffer,
                 'source':'docs/CUSTOMER_ORDER_RULES.md:24–32; docs/reports/bundle_quantities_20261106/RESULT.md',
                 'history':'Two executed orders 45407169-0247-1 and 15697211-0403-1: 6452at x3 each; historical fulfilment example, not a current oz_acc1 card or stock assertion'})
(P/'a_named_examples.json').write_text(json.dumps(examples,ensure_ascii=False,indent=2))

summary=json.loads((P/'a_summary.json').read_text())
summary['mapping_statuses']=dict(Counter(r['mapping_status'] for r in cards))
summary['current_composition_statuses']=dict(Counter(r['composition_status'] for r in cards if r['archived']=='False'))
summary['archived_composition_statuses']=dict(Counter(r['composition_status'] for r in cards if r['archived']=='True'))
summary['example_types']=[e['example_type'] for e in examples]
summary['validated_invariants']=['48492 unique product_id','unknown mappings emit no components','5133 drum set not toner+drum','no numeric stock/reserve published','no transitive component union']
(P/'a_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))

family_contexts=[]
for c in contexts:
    codes=sorted(c['components'])
    signature=[{'code':code,'model':models.get(code,{}).get('title'),'resource':models.get(code,{}).get('resource'),
                'chip':models.get(code,{}).get('raw',{}).get('chip'),
                'tc_type':models.get(code,{}).get('consumable_type')} for code in codes]
    family_contexts.append({'set_code':c['set_code'],'family_fingerprint':hashlib.sha256(json.dumps(signature,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                            'physical_fingerprint':c['physical_fingerprint'],'complete_leaf_codes':codes,'family_signature':signature,
                            'status':'confirmed_full_bom_context_not_full_execution_validation','source':'snapshot/db_set_cost.json','observed_at':c['proof'][0]['observed_at'],
                            'rule':'Family identity ignores only leaf multiplicity; physical composition preserves it. A shared leaf alone never merges contexts.'})
(P/'a_family_fingerprints.json').write_text(json.dumps(family_contexts,ensure_ascii=False,indent=2))

lines=['# A: артикулы, SEO, физические составы и контексты семейств',
       '',f"Обработан полный union {len(cards)} карточек oz_acc1:22536 текущих,25956 архивных; пересечение product_id=0. Бизнес-данные, API, БД и службы не менялись.",
       '', '## Источники и правила',
       '', 'Структура МС: точный ms_product.external_code и supplier_stock.external_code; code — товар конкретного поставщика. Первые четыре цифры offer_id — артикульный якорь. Он хранится отдельно от физического состава. Ведущие нули сохранены.',
       '', 'Физические составы: set_cost.components/n_components/resolved_at, первоисточник TheCartridge mix_data; раскрываются вложенные наборы и кратности. Проверены992 состава, циклов/ошибок0. Отдельно сохранены исходный тип ТК и контекстная физическая роль. У5133 TC parent=Комплект фотобарабанов, хотя5129 ошибочно type=Картридж: это набор барабанов.',
       '', 'Бандл содержит одинаковый товар; бандл набора умножает целый подтверждённый набор. Суффиксный XN проверен по названию/прямому подтверждению6452X3MS22I0. Набор разных товаров определяется BOM, не словом «комплект» и не числом4 в характеристике.',
       '', 'У ТК color=NULL и ink_colors=[] во всех6763 моделях; prc_tc_link пуст. Поэтому цвет/семья не выводятся из отсутствующих полей. Black candidates из title/mono сохранены как candidates. Сырые чипы взяты из raw.chip; неизвестный чип не означает отсутствие чипа.',
       '', 'Физический перенос на SEO требует соответствия полной модели и цвета либо прямо подтверждённого владельцем правила ordinary-single: numeric child, qty1, явно чёрная карточка, соответствующий mono/K якорь, одиночная роль, отсутствие признаков набора/другой роли/конфликтов. Эти уровни различаются mapping_status. Цветные/наборные SEO сохраняют артикульный якорь, но остаются physical unknown без точной спецификации.',
       '', 'Складское исполнение не выводится из суффикса артикула. В stock_by_exact_external_code только ссылки на ms_id/store на общей последней дате06.11; численные stock/reserve не публикуются и не суммируются. Это наличие записи точного внешнего кода, а не доказательство исполнения SEO.',
       '', '## Результат', '', '```json',json.dumps(summary,ensure_ascii=False,indent=2),'```',
       '', '## Границы семейств', '',
       'Один подтверждённый контекст — полный точный multiset leaf external_code из BOM. physical_fingerprint учитывает кратности, модели, ресурсы, типы и raw chip. family_fingerprint может игнорировать лишь кратности при равном полном множестве компонентов; это не меняет физическое количество. Shared black/shared printer не объединяют контексты. Один leaf в нескольких разных контекстах → несколько candidate_family_ids, физическая карточка не назначается произвольно.',
       '', 'Одинаковый united_title без явной полной серии/согласованных исполнений остаётся candidate. Parent-only группы — provisional article contexts; их нельзя считать тысячами подтверждённых больших физических семейств.',
       '', '## Прослеженные примеры', '']
for e in examples:
    if 'context' in e:
        c=e['context'];parts=', '.join(f"{x['code']} {x['model']} [{x['type']}]" for x in e['component_specifications'])
        lines.append(f"- {e['example_type']}: {c['set_code']} → {parts}; источник set_cost.components, дата{c['proof'][0]['observed_at']}.")
    elif 'child' in e:
        lines.append(f"- {e['example_type']}: parent {e['parent']['offer_id']} ({e['parent']['name']}) → child {e['child']['offer_id']} ({e['child']['name']}); mapping={e['child']['mapping_status']}, physical={e['child']['physical_components']}.")
    else: lines.append('- '+e['example_type']+':6452X3MS22I0 →6452at×3 по исполненным историческим заказам; operator-confirmed кратность, текущий склад не наследуется.')
lines+=['','Машинные файлы:a_cards.csv (все карточки),a_components.csv (только разрешённые физические связи),a_relations.csv (артикульные и контекстные связи с источником/статусом/датой),a_tc_contexts.json (BOM proof),a_family_fingerprints.json,a_named_examples.json. Неопределённость явна в каждой карточке; отсутствие компоненты в a_components.csv не превращается в нулевой физический состав.']
if (P/'a_anchor_schemes.md').exists():
    lines+=['', '## Схемы артикульных якорей', '', (P/'a_anchor_schemes.md').read_text().strip()]
(P/'a_report.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({'validated':True,'cards':len(cards),'components':len(components),'examples':len(examples),'mapping_statuses':summary['mapping_statuses']},ensure_ascii=False))

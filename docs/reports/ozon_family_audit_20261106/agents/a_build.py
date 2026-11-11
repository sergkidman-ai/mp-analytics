# поток: ozon-family; offline read-only audit of shared immutable snapshots.
from __future__ import annotations
import ast
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(__file__).parent
SNAP = OUT.parent / 'snapshot'

def read(name):
    return json.loads((SNAP / name).read_text())

def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))

def text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def csvwrite(name, rows):
    keys = list(rows[0]) if rows else ['empty']
    with (OUT / name).open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows({k: text(v) if isinstance(v, (dict, list)) else v for k,v in r.items()} for r in rows)

def parse_components(value):
    result = Counter()
    if not isinstance(value, (list, dict)):
        raise ValueError('unsupported_components_container')
    entries = value.items() if isinstance(value, dict) else ((v, 1) for v in value)
    for code, quantity in entries:
        if isinstance(code, str) and code.lstrip().startswith('{'):
            if len(code) > 8192:
                raise ValueError('component_too_large')
            code = ast.literal_eval(code)
        if isinstance(code, dict):
            code = code.get('external_code')
        if not isinstance(code, str) or not re.fullmatch(r'\d{4}', code):
            raise ValueError('component_not_exact_four_digit_code')
        if isinstance(quantity, bool) or float(quantity) <= 0 or float(quantity) != int(float(quantity)):
            raise ValueError('component_quantity_invalid')
        result[code] += int(float(quantity))
    return result

def flatten(code, boms, path=()):
    if code in path:
        raise ValueError('composition_cycle:' + '/'.join(path + (code,)))
    if code not in boms:
        return Counter({code:1}), []
    row = boms[code]
    immediate = parse_components(row['components'])
    if not immediate:
        raise ValueError('empty_bom')
    if row.get('n_components') != sum(immediate.values()):
        raise ValueError('n_components_mismatch')
    out, proof = Counter(), [{'external_code':code, 'source':'set_cost.components',
                              'observed_at':row['resolved_at'], 'components':dict(immediate)}]
    for child, n in immediate.items():
        child_bom, child_proof = flatten(child, boms, path + (code,))
        out.update({k:v*n for k,v in child_bom.items()})
        proof.extend(child_proof)
    return out, proof

def role(model):
    t = model.get('consumable_type') or ''
    # TC contains known role errors (e.g. black OKI C612 drum 5129 labelled
    # "Картридж"). A cartridge word alone does not establish toner identity.
    name = ' '.join(str(model.get(k) or '') for k in ['title','united_title','similar_title'])
    if re.search(r'фото\s*барабан|\bdrum\b|драм[- ]?картридж',name,re.I):
        return 'drum' if t in {'Фотобарабан','Картридж','Тонер-картридж'} else t
    if t == 'Фотобарабан': return 'drum'
    if t in {'Картридж', 'Тонер-картридж'}: return 'cartridge'
    if t == 'Тонер': return 'refill_toner'
    if t == 'Чернила': return 'ink'
    return t

def model_color(model):
    # Parsed colors are all NULL in this snapshot; preserve the actual evidence.
    if model.get('color'):
        return model['color'], 'prc_tc_model.color', 'explicit'
    name = str(model.get('title') or '')
    if re.search(r'(?:\bblack\b|\bBK\b|\bч[её]рн\w*|\bK\b)', name, re.I):
        return 'K', 'prc_tc_model.title', 'candidate_explicit_title'
    if re.search(r'(?:[0-9]K|BK|KE|BKE)$', name, re.I):
        return 'K', 'prc_tc_model.title model suffix', 'candidate_model_suffix'
    # Mono is useful for black-anchor candidates, but not proof of color identity.
    if model.get('raw', {}).get('chromaticity') == 'Черно-белый' and role(model) in {'cartridge','ink','refill_toner'}:
        return 'K', 'prc_tc_model.raw.chromaticity', 'candidate_monochrome'
    return '', '', 'unknown'

def ozon_color(value):
    value = ' '.join(value).lower()
    colors=[]
    for color, pat in [('K',r'black|черн|чёрн'),('C',r'cyan|голуб'),('M',r'magenta|пурпур|маджент'),('Y',r'yellow|желт|жёлт')]:
        if re.search(pat,value):colors.append(color)
    return colors[0] if len(colors)==1 else 'MULTI' if colors else ''

def attr_projection():
    result={}
    for f in sorted(SNAP.glob('attrs_[0-9]*.json')):
        page=json.loads(f.read_text())
        for card in page['body']['result']:
            attrs={int(a['id']):[str(v.get('value') or '') for v in a.get('values',[])] for a in card['attributes']}
            result[str(card['id'])]={'color_values':attrs.get(9602,[]),'quantity_values':attrs.get(8513,[]),
                                     'source':str(f.relative_to(OUT.parent)),'observed_at':page['observed_at']}
    return result

def main():
    catalog = read('catalog.json')
    if (SNAP / 'catalog_archived.json').exists():
        current_ids = {str(c['id']) for c in catalog}
        catalog += [c for c in read('catalog_archived.json') if str(c['id']) not in current_ids]
    models = {r['external_code']:r for r in read('db_prc_tc_model.json')}
    attrs=attr_projection()
    boms = {r['external_code']:r for r in read('db_set_cost.json')}
    provenance = read('catalog_provenance.json')
    if (SNAP / 'catalog_archived_provenance.json').exists():
        provenance += read('catalog_archived_provenance.json')
    prov = {str(r['product_id']):r for r in provenance}
    ms = defaultdict(list)
    for r in read('db_ms_product.json'):
        ms[r['external_code']].append(r)
    stock = defaultdict(list)
    stock_source=read('db_supplier_stock.json')
    stock_date=max(r['captured_at'] for r in stock_source)
    stock_seen=set()
    for r in stock_source:
        key=(r['ms_id'],r['store'],r['captured_at'])
        if r['captured_at']==stock_date and key not in stock_seen:
            stock_seen.add(key)
            stock[r['external_code']].append(r)
    contexts, bom_errors = {}, []
    for code, row in boms.items():
        try:
            flat, proof = flatten(code, boms)
        except (ValueError, TypeError, SyntaxError) as exc:
            bom_errors.append({'code':code,'error':str(exc)}); continue
        roles = {role(models.get(c, {})) for c in flat}
        parent_type = models.get(code,{}).get('consumable_type')
        black = [{'code':c, 'color':model_color(models.get(c,{}))[0],
                  'source':model_color(models.get(c,{}))[1], 'status':model_color(models.get(c,{}))[2]}
                 for c in flat if parent_type!='Комплект фотобарабанов' and model_color(models.get(c,{}))[0] in {'K','BK'} and role(models.get(c,{})) in {'cartridge','ink','refill_toner'}]
        exact_signature = [{'code':c, 'quantity':n, 'model':models.get(c,{}).get('title'),
                            'consumable_type':models.get(c,{}).get('consumable_type'),
                            'resource':models.get(c,{}).get('resource'),
                            'chip':models.get(c,{}).get('raw',{}).get('chip'),
                            'assignment':models.get(c,{}).get('raw',{}).get('assignment')}
                           for c,n in sorted(flat.items())]
        identity = text(exact_signature)
        # Both distinct component roles AND the explicit parent type are
        # required. Mixed TC member-role errors must not invent toner+drum.
        drum_cartridge = parent_type=='Фотобарабан + картридж' and {'drum','cartridge'} <= roles
        kind = 'homogeneous_pack' if len(flat)==1 and sum(flat.values())>1 else 'single_component_reference' if len(flat)==1 else 'drum_and_cartridge_set' if drum_cartridge else 'heterogeneous_drum_set' if parent_type=='Комплект фотобарабанов' else 'heterogeneous_set'
        contexts[code] = {'set_code':code, 'title':models.get(code,{}).get('title'),
                          'kind':kind, 'components':dict(flat), 'proof':proof,
                          'black_anchor_candidates':black, 'black_anchor_status':'candidate_needs_color_verification' if len(black)==1 else 'multiple_or_unknown_no_anchor',
                          'physical_fingerprint':hashlib.sha256(identity.encode()).hexdigest(),
                          'fingerprint_components':exact_signature, 'union_policy':'exact_full_multiset_only_no_shared_component_union'}

    cards, components, relations = [], [], []
    examples, seen_examples = [], set()
    for card in sorted(catalog, key=lambda r:str(r['offer_id'])):
        offer, name = str(card['offer_id']), card.get('name') or ''
        source = prov.get(str(card['id']), {})
        at = source.get('observed_at') or card.get('updated_at')
        anchor = re.match(r'^(\d{4})', offer)
        anchor = anchor[1] if anchor else ''
        match = re.fullmatch(r'(\d{4})[XХ](\d+)([A-Z0-9]*)', offer, re.I)
        mult, multiplier_status = 1, 'not_bundle_marker'
        if match and int(match[2])>1:
            qty = re.search(r'\b(\d+)\s*шт\b', name, re.I)
            if not match[3] or offer.upper()=='6452X3MS22I0' or (qty and int(qty[1])==int(match[2]) and re.match(r'^(?:Комплект|Бандл)\b',name,re.I)):
                mult = int(match[2]); multiplier_status='confirmed_article_marker' if not match[3] else 'operator_confirmed' if offer.upper()=='6452X3MS22I0' else 'confirmed_article_marker_and_title'
            else:
                multiplier_status='unconfirmed_suffixed_XN'
        article_kind = 'primary_exact_four_digits' if re.fullmatch(r'\d{4}',offer) else 'seo_numeric_child' if re.fullmatch(r'\d{5,}',offer) else 'bundle_marker' if match and mult>1 else 'suffix_requires_review' if anchor else 'unrecognized'
        identity_code = offer if offer in models or offer in boms else anchor
        mapping_status = 'exact_catalog_code' if offer == identity_code and (identity_code in models or identity_code in boms) else 'article_convention_bundle' if mult>1 else 'article_convention_seo' if article_kind=='seo_numeric_child' else 'article_prefix_only_unconfirmed' if anchor else 'unresolved'
        model = models.get(identity_code,{})
        ctx = contexts.get(identity_code)
        unknown_set = bool(re.search(r'комплект|набор|фотобарабан \+',model.get('consumable_type') or '',re.I))
        attr=attrs.get(str(card['id']),{})
        card_color=ozon_color(attr.get('color_values',[]))
        tc_color,tc_color_source,tc_color_status=model_color(model)
        normalized=lambda v:re.sub(r'[^A-ZА-ЯЁ0-9]','',str(v).upper())
        exact_model_in_name=bool(len(normalized(model.get('title')))>3 and normalized(model.get('title')) in normalized(name))
        title_set=bool(re.search(r'\b(?:набор|комплект|картриджи|фотобарабаны)\b',name,re.I))
        title_drum=bool(re.search(r'фото\s*барабан|драм[- ]?картридж|\bdrum\b',name,re.I))
        explicit_set_model=bool(ctx and len(ctx['components'])>1)
        veto=[]
        quantities=[int(q) for q in attr.get('quantity_values',[]) if re.fullmatch(r'\d+',q)]
        if mult==1 and not explicit_set_model and any(q>1 for q in quantities):veto.append('ozon_quantity_multiple_but_anchor_single')
        if title_set and mult==1 and not explicit_set_model:veto.append('title_says_set_but_anchor_is_single_or_unknown')
        if card_color and tc_color and card_color!=tc_color:veto.append('ozon_color_differs_from_anchor')
        if title_drum and role(model)=='cartridge':veto.append('ozon_role_drum_tc_role_cartridge')
        if article_kind in {'seo_numeric_child','suffix_requires_review'} or (match and match[3]):
            verified_specs=exact_model_in_name and bool(card_color and tc_color and card_color==tc_color) and not veto
            ordinary_single=(article_kind=='seo_numeric_child' and quantities==[1]
                             and card_color=='K' and tc_color=='K'
                             and role(model) in {'cartridge','ink','refill_toner'}
                             and not title_set and not title_drum and not explicit_set_model and not veto)
            if ordinary_single and not verified_specs:
                mapping_status='owner_article_rule_ordinary_single'
            elif not verified_specs:
                mapping_status='article_anchor_only_physical_unknown'
                if not exact_model_in_name:veto.append('full_anchor_model_not_in_name')
                if not(card_color and tc_color):veto.append('color_not_verified_on_both_sides')
            else:
                mapping_status='verified_model_color_article_mapping'
        elif veto and offer!=identity_code:
            mapping_status='article_anchor_conflicting_specs_physical_unknown'
        mapping_ok = mapping_status in {'exact_catalog_code','article_convention_bundle','verified_model_color_article_mapping','owner_article_rule_ordinary_single'}
        if not mapping_ok:
            composition,composition_status,product_kind,bom_source,bom_at={},'unknown_physical_mapping','unresolved','',''
        elif ctx:
            composition = {c:n*mult for c,n in ctx['components'].items()}
            composition_status = 'confirmed_bom_exact_code' if mapping_status=='exact_catalog_code' else 'confirmed_bom_article_mapping' if mapping_ok else 'candidate_bom_prefix_only'
            product_kind = ('bundle_of_sets' if len(ctx['components'])>1 else 'homogeneous_bundle') if mult>1 else ctx['kind']
            bom_source = 'set_cost.components'
            bom_at = boms[identity_code]['resolved_at']
        elif model and not unknown_set:
            composition = {identity_code:mult}
            composition_status = 'exact_single_catalog_item' if mapping_status=='exact_catalog_code' else 'single_article_mapping' if mapping_ok else 'candidate_single_prefix_only'
            product_kind = 'homogeneous_bundle' if mult>1 else 'single'
            bom_source = 'prc_tc_model.consumable_type; article convention'
            bom_at = model.get('last_seen')
        else:
            composition, composition_status, product_kind, bom_source, bom_at = {}, 'unresolved_missing_bom' if unknown_set else 'unresolved_missing_tc_model', 'unresolved', '', ''
        stock_rows = stock.get(offer,[])
        stock_by_store = defaultdict(list)
        for r in stock_rows:
            stock_by_store[str(r['store'])].append({'ms_id':r['ms_id'],'captured_at':r['captured_at'],
                                                    'source':'snapshot/db_supplier_stock.json'})
        row = {'product_id':str(card['id']), 'sku':str(card.get('sku') or ''), 'offer_id':offer, 'name':name,
               'archived':bool(card.get('is_archived')), 'article_kind':article_kind,
               'article_anchor':anchor, 'identity_code':identity_code, 'mapping_status':mapping_status,
               'seo_flag':article_kind=='seo_numeric_child', 'seo_suffix_candidate':bool(match and match[3]),
               'ozon_color':card_color,'ozon_color_values':attr.get('color_values',[]),
               'ozon_quantity_values':attr.get('quantity_values',[]),'attrs_source':attr.get('source'),
               'attrs_observed_at':attr.get('observed_at'),'model_in_name':exact_model_in_name,
               'anchor_color':tc_color,'anchor_color_source':tc_color_source,'mapping_veto':veto,
               'multiplier':mult,'multiplier_status':multiplier_status,'product_kind':product_kind,
               'composition_status':composition_status,'physical_components':composition,
               'tc_type':model.get('consumable_type'),'tc_model':model.get('title'),
               'tc_series_label':model.get('united_title'),'tc_resource':model.get('resource'),
               'tc_chip_raw':model.get('raw',{}).get('chip'),
               'black_anchor_candidates':ctx['black_anchor_candidates'] if ctx else [],
               'physical_fingerprint':ctx['physical_fingerprint'] if ctx else '',
               'exact_ms_cards':len(ms.get(offer,[])), 'exact_ms_live_cards':sum(not r['archived'] for r in ms.get(offer,[])),
               'stock_by_exact_external_code':dict(stock_by_store), 'stock_status':'exact_external_code_exists_latest_snapshot_not_seo_execution_proof' if stock_rows else 'no_exact_code_rows_latest_snapshot_not_proof_of_zero',
               'warehouse_suffix_observation':offer[4:] if anchor and article_kind=='suffix_requires_review' else '',
               'warehouse_status':'exact_stock.store_only; suffix_does_not_prove_warehouse',
               'source':source.get('source','catalog snapshot'), 'observed_at':at,
               'composition_source':bom_source,'composition_observed_at':bom_at,
               'argument':'Exact BOM with multiplicities; article parent and physical composition kept separate; no printer/shared-component transitive union'}
        cards.append(row)
        if anchor and offer!=anchor:
            relations.append({'from_offer_id':offer,'to_external_code':anchor,'relation_type':'bundle_to_article_anchor' if mult>1 else 'seo_to_article_anchor' if article_kind=='seo_numeric_child' else 'prefix_to_article_anchor',
                              'status':mapping_status,'quantity':mult,'source':'offer_id article convention; docs/CUSTOMER_ORDER_RULES.md:14–15,24–32','observed_at':at,'argument':'Article relationship only; no implied set composition or warehouse'})
        for code,quantity in composition.items():
            components.append({'product_id':str(card['id']),'offer_id':offer,'identity_code':identity_code,
                               'component_code':code,'quantity':quantity,'component_model':models.get(code,{}).get('title'),
                               'component_role':'drum' if ctx and ctx['kind']=='heterogeneous_drum_set' else role(models.get(code,{})),
                               'component_role_source':'TC parent is Комплект фотобарабанов; member TC type may conflict' if ctx and ctx['kind']=='heterogeneous_drum_set' else 'prc_tc_model.consumable_type + explicit role title',
                               'source_tc_component_type':models.get(code,{}).get('consumable_type'),'status':composition_status,
                               'mapping_status':mapping_status,'source':bom_source,'observed_at':bom_at,
                               'card_source':row['source'],'card_observed_at':at,
                               'argument':'BOM proof in a_tc_contexts.json' if ctx else 'Single TC consumable; no heterogeneous composition asserted'})
        key = product_kind if product_kind not in {'single','unresolved'} else article_kind
        if key not in seen_examples:
            examples.append({'example_type':key,'card':row,'bom_context':ctx,'model_source':model,'ms_exact_examples':ms.get(offer,[])[:3]});seen_examples.add(key)
    # Independently retained edges for black anchors and drum/toner sets.
    for code,ctx in contexts.items():
        for black in ctx['black_anchor_candidates']:
            relations.append({'from_offer_id':code,'to_external_code':black['code'],'relation_type':'black_anchor_candidate_within_exact_bom','status':black['status'],'quantity':ctx['components'][black['code']],
                              'source':'set_cost.components + '+black['source'],'observed_at':boms[code]['resolved_at'],'argument':'Set-specific context only; never unions other sets sharing this black'})
        if ctx['kind']=='drum_and_cartridge_set':
            for comp,n in ctx['components'].items():
                relations.append({'from_offer_id':code,'to_external_code':comp,'relation_type':'drum_cartridge_set_component','status':'confirmed_tc_bom','quantity':n,'source':'set_cost.components','observed_at':boms[code]['resolved_at'],'argument':'Exact set contains drum/cartridge; does not assert interchangeable consumables'})
    csvwrite('a_cards.csv',cards);csvwrite('a_components.csv',components);csvwrite('a_relations.csv',relations)
    save('a_tc_contexts.json',list(contexts.values()));save('a_traced_examples.json',examples);save('a_bom_errors.json',bom_errors)
    summary={'cards':len(cards),'unique_product_ids':len({r['product_id'] for r in cards}),
             'article_kinds':dict(Counter(r['article_kind'] for r in cards)),
             'product_kinds':dict(Counter(r['product_kind'] for r in cards)),
             'composition_statuses':dict(Counter(r['composition_status'] for r in cards)),
             'tc_contexts':len(contexts),'tc_context_types':dict(Counter(c['kind'] for c in contexts.values())),
             'component_rows':len(components),'relation_rows':len(relations),'bom_errors':len(bom_errors),
             'tc_color_present':sum(bool(m.get('color')) for m in models.values()),
             'tc_raw_colors_present':sum(bool(m.get('raw',{}).get('ink_colors')) for m in models.values()),
             'tc_link_rows':len(read('db_prc_tc_link.json')),
             'card_stock_code_exists_latest':sum(bool(r['stock_by_exact_external_code']) for r in cards),
             'no_business_writes':True,'no_transitive_union':True}
    save('a_summary.json',summary)
    print(text(summary))

if __name__=='__main__':main()

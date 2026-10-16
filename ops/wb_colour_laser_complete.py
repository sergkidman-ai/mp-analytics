#!/usr/bin/env python3
# поток: mkt
"""Full acc1 colour-laser census; exact identities, no stock filter, explicit apply.

TC printer lists describe compatibility, not series identity. Series are built
from explicit model families, then actual WB compatibility and chip variants
are checked before any move. Missing expected codes are searched across ALL
primary cards by exact model; a loose spelling match is review, never creation.
"""
import argparse
from collections import Counter, defaultdict
import csv
import fcntl
from functools import lru_cache
import hashlib
import html
import json
from pathlib import Path
import re
import time

from core import db
from ops import wb_bundle_regroup as regroup, wb_core_glue as core
from ops.wb_bundle_plan import char, color, fetch_catalog, fingerprint, is_bundle, normalized, quantities, number
from ops.wb_bundle_glue_singletons import Content, now, read_references
from ops.wb_component_gap_scan import laser, plain_card
from ops.wb_glue_recovery import entries, make_fresh, retryable

OUT = regroup.ROOT / 'docs/reports/mkt_wb_colour_laser_complete_2026-10-16_acc1'
COLORS = ('K', 'C', 'M', 'Y')
LABELS = {'K': 'чёрный', 'C': 'голубой', 'M': 'пурпурный', 'Y': 'жёлтый'}
# A TC assignment error is independently resolved by the manufacturer's own
# product classification. This exclusion is analysis-only, never a TC edit.
TECHNIQUE_EXCLUSIONS = {'981': 'https://www.hp.com/us-en/shop/mdp/ink--toner---paper/hp-981-ink-cartridges'}
TECHNIQUE_EXCLUSIONS['T9741(C13T974100)DS'] = 'https://epson.com/For-Home/Ink/Epson-T974-DURABrite-Pro-Ink/i/T974120'
ROLE_EXCLUSIONS = {'4718': 'Пять товаров поставщиков называют эту модель DRUM/драм-картриджем',
                   '5134': 'Поставщик прямо называет модель 43913807 drum',
                   '5513': 'Поставщики: IU-214C — блок формирования изображения/проявки',
                   '5514': 'Поставщики: IU-214M — блок проявки', '5515': 'Поставщики: IU-214Y — блок проявки'}
BLACK_RELATIONS = {
    'Q2681AQ2683ADS': ('Q2670A', 'https://support.hp.com/in-en/document/c02639296'),
    'Q7581AQ7583ADS': ('Q6470A', 'https://www.hp.com/pageyield/kz/ru/CP3505/index.html'),
    '106R03485106R034882400': ('106R03484', 'https://www.xerox.com/en-fk/office/printers/phaser-6510/software'),
    '106R0108011501152DS': ('106R01080', 'ТК: модель чёрного явно названа в united_title семейства'),
    'CEXV58L': ('C-EXV58', 'https://oip.manual.canon/USRMA-5508-zz-CS-5800-ruRU/contents/devu-int-mainte-repl_parts.html'),
    '717': ('711BK', 'https://colour-laser-printers.co.uk/pdfs/2233B010AA.pdf'),
}
MISSING_BLACK_RELATIONS = {'78C5X': ('78C5XKE', 'https://www.lexmark.com/en_xm/printers/supply/12626/Lexmark-78C5XKE-Black-Extra-High-Yield-Contract-Toner-Cartri')}
ADJUDICATED = {
    'Kyocera TK-5370 XL — K/Y': {'codes': ['6773', '6774'], 'evidence': 'Точные TK-5370K XL и TK-5370Y XL в справочнике, остальные цвета XL там не найдены'},
    'Ricoh SPC252 — имеющиеся K/M': {'codes': ['6090', '5962'], 'evidence': 'Карточки уже в одной группе WB; название SPC352E у чёрного требует проверки, это не разрешение переносить другие модели',
        'warning': 'У чёрного6090 модель SPC352E, у пурпурного5962 SP-C252EM. Сверить наименования поставщиков; не подменять имеющуюся пару серией HE другого ресурса.'},
    'Q6470A-Q6473A DS': {'codes': ['5960'], 'evidence': 'Модель ТК Q6471A/711C прямо содержит Q6471A; WB-карточка названа для HP LJ3600. Это не разрешение обходить полное сравнение принтеров и чипов перед переносом.'},
    'Xerox C7020 — metered 106R03733–106R03736': {'codes': ['5501'], 'evidence': 'https://www.office.xerox.com/latest/VC7SS-012.PDF',
        'missing': {'K': '106R03733', 'C': '106R03736', 'M': '106R03735'},
        'warning': 'OEM относит 106R03734 к metered, ресурс15000; у нас указан16500. Не смешивать с обычными 106R03737–106R03740 без проверки модели и чипа.'},
}
FBS = {}


def model_key(value):
    return normalized(value)


def spelling_key(value):
    # Diagnostic only; this key NEVER authorizes a marketplace move.
    return re.sub(r'\d+', lambda m: str(int(m[0])), model_key(value))


@lru_cache(maxsize=2048)
def kit_tokens(title):
    return tuple(core.tokens(title) or [])


def kit_matches(ref, components):
    toks = kit_tokens(ref.get('title'))
    if not toks or not components:
        return False
    def match(t, r):
        virtual = {'characteristics': [{'id': 58854, 'value': [LABELS.get(color(r.get('color')), '')]}]}
        return core.model_matches(t, virtual, r)
    return all(any(match(t, r) for t in toks) for r in components) and all(any(match(t, r) for r in components) for t in toks)


def colour_template(singles):
    """Prove the one C/M/Y position, preserving every resource/model suffix."""
    names = [{model_key(r['title']) for r in singles if color(r.get('color')) == co} for co in ('C', 'M', 'Y')]
    if not all(len(v) == 1 for v in names): return None
    c, m, y = [next(iter(v)) for v in names]
    if len({len(c), len(m), len(y)}) != 1: return None
    positions = [i for i in range(len(c)) if (c[i], m[i], y[i]) == ('C', 'M', 'Y') and c[:i] == m[:i] == y[:i] and c[i + 1:] == m[i + 1:] == y[i + 1:]]
    if len(positions) != 1: return None
    i = positions[0]
    return c[:i], c[i + 1:]


def collect():
    OUT.mkdir(parents=True, exist_ok=True)
    cards = fetch_catalog('wb_acc1')
    regroup.save(OUT / 'catalog.json', {'account': 'wb_acc1', 'read_at': now(), 'cards': cards})
    refs, marks = read_references({regroup.base(c) for c in cards} - {None})
    regroup.save(OUT / 'references.json', {'references': refs, 'marks': {k: sorted(v) for k, v in marks.items()}, 'read_at': now()})
    regroup.save(OUT / 'all_models.json', db.query('select external_code,title,consumable_type,color,resource,chip,printer_models,united_title,similar_title,raw,first_seen,gone_at from prc_tc_model'))
    print('Fresh catalog saved', len(cards), flush=True)


def make_operation(selected, own, groups, family, variant, references=None):
    cards = [r['card'] for r in selected]
    reasons = []
    if any(r['chip'] == 'противоречие' for r in selected):
        reasons.append('Явные источники противоречат друг другу по чипу')
    compatible = {core.printers(r['card'], r['ref']) for r in selected}
    if len(compatible) != 1 or not next(iter(compatible), ('', ()))[1]:
        reasons.append('Полные списки принтеров компонентов WB не совпадают либо не заполнены')
    for rec in selected:
        c, ref = rec['card'], rec['ref']
        if model_key(char(c, 5023)) != model_key(ref.get('title')):
            reasons.append(c['vendorCode'] + ': модель в характеристике WB отличается от модели справочника')
        if ref['consumable_type'] in regroup.CARTRIDGES:
            if color(char(c, 58854)) != color(ref.get('color')):
                reasons.append(c['vendorCode'] + ': цвет WB отличается от справочника')
            if number(char(c, 88963)) and number(ref.get('resource')) and number(char(c, 88963)) != number(ref['resource']):
                reasons.append(c['vendorCode'] + ': ресурс WB отличается от справочника; исполнение не подтверждено')
        else:
            declared_count = number((ref.get('raw') or {}).get('colors_count'))
            if declared_count not in (3, 4) or quantities(c)[1] != declared_count:
                reasons.append(c['vendorCode'] + ': количество компонентов WB не совпадает с подтверждённым составом набора ТК')
    codes = {c['vendorCode'] for c in cards}
    touched = {c['imtID'] for c in cards}
    for rec in selected:
        bb = own.get(rec['card']['vendorCode'], [])
        if bb:
            reasons.extend(regroup.reasons(rec['card'], bb, rec['ref'], rec['chip']))
            touched.update(c['imtID'] for c in bb)
    whole = [c for root in sorted(touched) for c in groups[root]]
    core_ids = {c['nmID'] for c in cards}
    black = next((r['card'] for r in selected if r['ref']['consumable_type'] in regroup.CARTRIDGES and color(r['ref'].get('color')) == 'K'), None)
    anchor = black or cards[0]
    foreign = [c for c in whole if (not is_bundle(c) and c['nmID'] not in core_ids) or (is_bundle(c) and regroup.base(c) not in codes)]
    # Existing approved drum/toner accessories may stay at the black anchor's
    # root. They are NEVER moved from another group or silently reclassified.
    preserve = []
    for c in foreign:
        ref = (references or {}).get(regroup.base(c), {})
        if c['imtID'] == anchor['imtID'] and ref.get('consumable_type') in {'Фотобарабан', 'Фотобарабан + картридж', 'Комплект фотобарабанов'} and core.printers(c, ref) in compatible:
            preserve.append(c)
    if len(preserve) != len(foreign):
        reasons.append('В исходной группе есть посторонние товары, которые нельзя переносить без отдельного разбора: ' + '; '.join(c['vendorCode'] + ' ' + c['title'] for c in foreign if c not in preserve))
    if reasons:
        return None, sorted(set(reasons))
    main = core_ids | {c['nmID'] for c in preserve}
    leftovers = defaultdict(list)
    for c in whole:
        if not is_bundle(c) or c in preserve:
            continue
        if quantities(c)[3] == 2:
            main.add(c['nmID'])
        else:
            leftovers[regroup.base(c)].append(c['nmID'])
    parts = [sorted(main), *[sorted(ids) for ids in leftovers.values()]]
    if max(map(len, parts)) > 30:
        return None, ['Состав превышает лимит WB 30 карточек']
    ident = int.from_bytes(hashlib.blake2b((family + '|' + variant).encode(), digest_size=8).digest(), 'big')
    op = {'id': ident, 'anchor': anchor['nmID'], 'title': family + ' / ' + variant,
          'rules': ['Цветные лазерные: все подтверждённые цвета и набор + свои X2; остаток не является фильтром'],
          'before_cards': whole, 'before_groups': {str(rt): sorted(c['nmID'] for c in groups[rt]) for rt in touched},
          'before_fingerprint': fingerprint(whole), 'partitions': parts,
          'preferred_roots': {str(anchor['imtID']): anchor['nmID']}}
    regroup.validate(op)
    return op, []


def build(snapshot, source, models):
    cards = snapshot['cards']
    models = list(models)
    # Some genuine four-model CMYK sets are mistyped as "Картридж" in TC.
    # Recognize only explicit four-model composition AND WB quantity four.
    # This changes the analysis role, never any source field.
    primary_cards = {c['vendorCode']: c for c in cards if not is_bundle(c)}
    for i, ref in enumerate(models):
        c = primary_cards.get(ref['external_code'])
        if c and ref['consumable_type'] in regroup.CARTRIDGES and not ref.get('color') and quantities(c)[1] == 4 and len(kit_tokens(ref['title'])) == 4 and model_key(char(c, 5023)) == model_key(ref['title']):
            labels = {color(v) for v in core.compatible_values(c, 58854)}
            if set(COLORS) <= labels or str((ref.get('raw') or {}).get('colors_count')) == '4':
                models[i] = {**ref, 'consumable_type': 'Комплект картриджей', 'source_consumable_type': ref['consumable_type'], 'raw': {**(ref.get('raw') or {}), 'colors_count': 4}}
    bycode = defaultdict(list)
    bymodel = defaultdict(list)
    byspelling = defaultdict(list)
    own, groups, tails = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in cards:
        groups[c['imtID']].append(c)
        if regroup.base(c) and c['vendorCode'] != regroup.base(c):
            tails[regroup.base(c)].append(c)
        if is_bundle(c):
            own[regroup.base(c)].append(c)
            continue
        bycode[c['vendorCode']].append(c)
        # A printer-titled child has a printer model in characteristic 5023.
        # It must not masquerade as a replacement main cartridge.
        if quantities(c)[1] in (1, 4) and not re.fullmatch(r'\d{5,}', c['vendorCode']):
            bymodel[model_key(char(c, 5023))].append(c)
            byspelling[spelling_key(char(c, 5023))].append(c)
    refs = {r['external_code']: r for r in models}
    candidate_cache = {}
    def candidates(ref):
        if ref['external_code'] in candidate_cache:
            return candidate_cache[ref['external_code']]
        # A second offer of the same model is not a "replacement" while the
        # exact expected vendor code is still active. Search by model only
        # when that expected primary has actually disappeared.
        candidates = bycode.get(ref['external_code'], []) or bymodel.get(model_key(ref['title']), [])
        typ = ref['consumable_type']
        out = {}
        for c in candidates:
            if is_bundle(c):
                continue
            if typ in regroup.CARTRIDGES:
                if quantities(c)[1] != 1 or color(char(c, 58854)) != color(ref.get('color')):
                    continue
                actual = refs.get(c['vendorCode'])
                if actual and actual['consumable_type'] not in regroup.CARTRIDGES:
                    continue
                # Same printed model can have different resources or an
                # erroneous technique label. It is not a proven replacement.
                if c['vendorCode'] != ref['external_code']:
                    if number(ref.get('resource')) and number(char(c, 88963)) and number(ref['resource']) != number(char(c, 88963)):
                        continue
                    if actual and 'струйн' in str((actual.get('raw') or {}).get('assignment', '')).lower() and laser(ref):
                        continue
            # A TC-confirmed set can have a wrong WB quantity field. Keep it
            # visible as EXISTING inventory; make_operation will block writes
            # until its four-component contents are proved.
            out[c['nmID']] = c
        candidate_cache[ref['external_code']] = list(out.values())
        return candidate_cache[ref['external_code']]
    def record(c, ref):
        actual = refs.get(c['vendorCode'], ref)
        evidence_ref = actual if model_key(actual['title']) == model_key(char(c, 5023)) and actual['consumable_type'] == ref['consumable_type'] else ref
        cl, ev = regroup.chip_info(c, own.get(c['vendorCode'], []), evidence_ref, source.get('marks', {}).get(c['vendorCode'], []))
        return {'card': c, 'ref': evidence_ref, 'chip': cl, 'evidence': ev, 'expected_code': ref['external_code']}
    eligible = [r for r in models if laser(r) and r['consumable_type'] in regroup.CARTRIDGES | {'Комплект картриджей'} and r['external_code'] not in ROLE_EXCLUSIONS]
    series = defaultdict(list)
    no_family, derived_families = [], []
    assigned_special = {code: (name, info) for name, info in ADJUDICATED.items() for code in info['codes']}
    for ref in eligible:
        if ref['external_code'] in assigned_special:
            label, info = assigned_special[ref['external_code']]
            series[model_key(label)].append({**ref, 'united_title': label})
            derived_families.append({'code': ref['external_code'], 'family': label, 'evidence': info['evidence']})
            continue
        if ref.get('united_title'):
            series[model_key(ref['united_title'])].append(ref)
        elif ref['consumable_type'] in regroup.CARTRIDGES and color(ref.get('color')) in {'C', 'M', 'Y'}:
            # A missing TC family field does not make an already complete WB
            # series incomplete. Derive only from an explicit co-grouped kit
            # model covering this exact cartridge; never from a printer alone.
            families = set()
            for c in candidates(ref):
                for other in groups[c['imtID']]:
                    kr = refs.get(other['vendorCode'], {})
                    if kr.get('consumable_type') == 'Комплект картриджей' and kr.get('united_title') and quantities(other)[1] == 4 and any(core.model_matches(t, c, ref) for t in kit_tokens(kr['title'])):
                        families.add(kr['united_title'])
            aliases = {model_key(part) for part in ref['title'].split('/')}
            if len(aliases) > 1:
                possible = [r for r in eligible if r.get('united_title') and r['consumable_type'] in regroup.CARTRIDGES and model_key(r['title']) in aliases and color(r.get('color')) == color(ref.get('color')) and number(r.get('resource')) == number(ref.get('resource'))]
                compatible = [r for r in possible if any(core.printers(c, ref) == core.printers(x, r) for c in candidates(ref) for x in candidates(r))]
                choices = compatible or possible
                if len({r['united_title'] for r in choices}) == 1:
                    families.add(choices[0]['united_title'])
            if len(families) == 1:
                family = next(iter(families)); series[model_key(family)].append({**ref, 'united_title': family})
                derived_families.append({'code': ref['external_code'], 'family': family, 'evidence': 'Точная модель входит в состав существующего набора в той же группе WB'})
                continue
            for c in candidates(ref):
                no_family.append({'card': plain_card(c), 'model': ref['title'], 'reason': 'В справочнике нет семейства: нужны точные модели остальных цветов и чёрного'})
    all_kits = [r for r in eligible if r['consumable_type'] == 'Комплект картриджей' and candidates(r)]
    non_toner_families = {model_key(refs[code].get('united_title', '')) for code in ROLE_EXCLUSIONS if code in refs}
    def color_kit_scope(ref):
        family = model_key(ref.get('united_title', ''))
        if family in TECHNIQUE_EXCLUSIONS or family in non_toner_families:
            return False
        count = number((ref.get('raw') or {}).get('colors_count'))
        # A two-pack of the same black cartridge, or a drum+toner pair, is
        # not an unassigned COLOUR set merely because TC calls it a set.
        if count is not None and count < 3:
            return False
        pieces = [model_key(x) for x in re.split(r'[/+]', ref['title'])]
        if len(pieces) >= 2 and all(any(model_key(r['title']) == name and 'струйн' in str((r.get('raw') or {}).get('assignment', '')).lower() for r in models) for name in pieces):
            return False
        return True
    all_kits = [r for r in all_kits if color_kit_scope(r)]
    kit_like = [c for c in cards if not is_bundle(c) and not re.fullmatch(r'\d{5,}', c['vendorCode']) and (quantities(c)[1] == 4 or refs.get(c['vendorCode'], {}).get('consumable_type') == 'Комплект картриджей' or re.search(r'^картриджи\b|набор\s+картридж', c.get('title', ''), re.I))]
    # TC sometimes repeats exactly the same physical model family under two
    # labels (e.g. a DS suffix, or only the M/Y subfamily). Merge only an exact
    # model+colour+resource subset with at least two shared colours. Different
    # resources or different models of any one colour block the union.
    parent = {k: k for k in series}
    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]; k = parent[k]
        return k
    signatures = {k: {(color(r.get('color')), model_key(r['title']), number(r.get('resource'))) for r in rr if r['consumable_type'] in regroup.CARTRIDGES and color(r.get('color')) in COLORS} for k, rr in series.items()}
    signature_owners = defaultdict(list)
    for key, sig in signatures.items():
        for item in sig: signature_owners[item].append(key)
    pairs = set()
    for owners in signature_owners.values():
        for a in owners:
            for b in owners:
                if a < b: pairs.add((a, b))
    for a, b in sorted(pairs):
        ra, rb = root(a), root(b)
        if ra == rb: continue
        sa, sb = signatures[ra], signatures[rb]
        combined = sa | sb
        if len({x[0] for x in sa & sb}) < 2 or not (sa <= sb or sb <= sa): continue
        if any(len({(x[1], x[2]) for x in combined if x[0] == co}) > 1 for co in COLORS): continue
        parent[rb] = ra; signatures[ra] = combined
    merged = defaultdict(list)
    for key, rr in series.items(): merged[root(key)].extend(rr)
    series = {key: list({r['external_code']: r for r in rr}.values()) for key, rr in merged.items()}
    # Fill a missing metadata colour from an explicit kit/range, or a proven
    # C/M/Y model template. Unlike matching a printer, these are model-identity
    # proofs. Conflicting model/resource options remain concrete review cases.
    for key, rr in series.items():
        singles = [r for r in rr if r['consumable_type'] in regroup.CARTRIDGES]
        if not any(color(r.get('color')) in {'C', 'M', 'Y'} for r in singles):
            continue
        template = colour_template(singles)
        family_label = re.sub(r'\s+DS$', '', str(rr[0].get('united_title', '')), flags=re.I)
        literal = list(kit_tokens(family_label))
        for kr in rr:
            if kr['consumable_type'] == 'Комплект картриджей': literal.extend(kit_tokens(kr['title']))
        for co in COLORS:
            if any(color(r.get('color')) == co for r in singles): continue
            template_names = {template[0] + tag + template[1] for tag in (['K', 'BK', ''] if co == 'K' else [co])} if template else set()
            if co == 'K' and key in BLACK_RELATIONS: template_names.add(model_key(BLACK_RELATIONS[key][0]))
            additions = []
            for ref in eligible:
                if ref['consumable_type'] not in regroup.CARTRIDGES or color(ref.get('color')) != co: continue
                virtual = {'characteristics': [{'id': 58854, 'value': [LABELS[co]]}]}
                if model_key(ref['title']) in template_names or any(core.model_matches(t, virtual, ref) for t in literal): additions.append(ref)
            identities = {(model_key(r['title']), number(r.get('resource'))) for r in additions}
            if len(identities) == 1:
                rr.extend(r for r in additions if r['external_code'] not in {x['external_code'] for x in rr})
                derived_families.extend({'code': r['external_code'], 'family': rr[0]['united_title'], 'evidence': 'Точная модель в составе набора/диапазоне или единственная модель по шаблону C/M/Y; ресурс сохранён'} for r in additions)
    rows, proposals, used = [], [], set()
    excluded_technique = [{'card': plain_card(c), 'reason': reason, 'source': None} for code, reason in ROLE_EXCLUSIONS.items() if code in refs for c in candidates(refs[code])]
    for family, rr in sorted(series.items()):
        singles = [r for r in rr if r['consumable_type'] in regroup.CARTRIDGES]
        if not any(color(r.get('color')) in {'C', 'M', 'Y'} for r in singles):
            continue
        if family in TECHNIQUE_EXCLUSIONS:
            excluded_technique.extend({'card': plain_card(c), 'reason': 'HP 981 — чернильная PageWide серия; ошибка назначения «лазерный» в справочнике', 'source': TECHNIQUE_EXCLUSIONS[family]} for r in singles for c in candidates(r))
            continue
        actual = {}
        for r in singles:
            for c in candidates(r):
                actual[c['nmID']] = record(c, r)
        if not actual:
            remaining = {c['nmID']: c for r in singles for c in tails.get(r['external_code'], [])}
            live_kits = {c['nmID']: c for r in rr if r['consumable_type'] == 'Комплект картриджей' for c in candidates(r)}
            if remaining or live_kits:
                missing = [{'color': co, 'status': 'absent_active_catalog' if any(color(r.get('color')) == co for r in singles) else 'model_unmapped',
                            'expected': [{'code': r['external_code'], 'model': r['title']} for r in singles if color(r.get('color')) == co], 'alternatives': []} for co in COLORS]
                rows.append({'family': rr[0]['united_title'], 'family_key': family, 'chip': 'исполнение основных не подтверждено', 'black': [],
                             'expected_black': [{'code': r['external_code'], 'model': r['title']} for r in singles if color(r.get('color')) == 'K'],
                             'components': [], 'kits': [plain_card(c) for c in live_kits.values()], 'other_execution_kits': [], 'missing_colors': missing, 'missing_kit': not live_kits,
                             'needs_kit_execution_review': False, 'full_colors': False, 'all_found_together': False, 'replacements': [],
                             'derivatives': [plain_card(c) for c in remaining.values()], 'reasons': ['Основных карточек нет, но остались наборы/бандлы/дочерние. Проверить намеренное удаление; без команды не восстанавливать.'], 'status': 'primary_cards_absent'})
            continue
        # Search a kit globally, not just under one TC united_title/printer list.
        kit_refs = [r for r in rr if r['consumable_type'] == 'Комплект картриджей']
        component_printers = {core.printers(rec['card'], rec['ref']) for rec in actual.values()}
        for kr in all_kits:
            # Model names such as TN-321 occur at Brother AND Konica Minolta.
            # A cross-family set must also agree with an actual whole WB
            # compatibility list; model spelling alone is not identity.
            if kr not in kit_refs and kit_matches(kr, singles) and any(core.printers(c, kr) in component_printers for c in candidates(kr)):
                kit_refs.append(kr)
        kit_records = {}
        for r in kit_refs:
            for c in candidates(r):
                kit_records[c['nmID']] = record(c, r)
        component_roots = {r['card']['imtID'] for r in actual.values()}
        possible_sets = []
        for c in kit_like:
            if c['nmID'] in kit_records or c['nmID'] in actual:
                continue
            ref = refs.get(c['vendorCode'], {})
            own_family = ref.get('united_title') and model_key(ref['united_title']) in {model_key(r.get('united_title', '')) for r in rr}
            explicit_model = ref and kit_matches(ref, singles) and core.printers(c, ref) in component_printers
            if c['imtID'] in component_roots or own_family or explicit_model:
                possible_sets.append(plain_card(c))
        all_rec = list(actual.values()) + list(kit_records.values())
        known = {r['chip'] for r in all_rec} - {regroup.UNKNOWN, 'противоречие'}
        variants = sorted(known) or [regroup.UNKNOWN]
        if known and known != {'с чипом'} and any(r['chip'] == regroup.UNKNOWN for r in all_rec):
            variants.append(regroup.UNKNOWN)
        if any(r['chip'] == 'противоречие' for r in all_rec):
            variants.append('противоречие')
        for variant in variants:
            allow_unknown = known == {'с чипом'} and variant == 'с чипом'
            chosen = [r for r in all_rec if r['chip'] == variant or (allow_unknown and r['chip'] == regroup.UNKNOWN)]
            components = [r for r in chosen if r['ref']['consumable_type'] in regroup.CARTRIDGES]
            if not components:
                continue
            kits = [r for r in chosen if r['ref']['consumable_type'] == 'Комплект картриджей']
            cols = {color(r['ref'].get('color')) for r in components}
            missing = []
            for co in COLORS:
                if co in cols:
                    continue
                expected = [r for r in singles if color(r.get('color')) == co]
                alternates = [plain_card(r['card']) for r in actual.values() if color(r['ref'].get('color')) == co]
                loose = {c['nmID']: plain_card(c) for r in expected for c in byspelling.get(spelling_key(r['title']), []) if color(char(c, 58854)) == co}
                state = 'different_execution_exists' if alternates else 'possible_replacement' if loose else 'absent_active_catalog' if expected else 'model_unmapped'
                missing.append({'color': co, 'status': state, 'expected': [{'code': r['external_code'], 'model': r['title']} for r in expected],
                                'alternatives': list({r['nm_id']: r for r in [*alternates, *loose.values()]}.values())})
            black = [r for r in components if color(r['ref'].get('color')) == 'K']
            roots = {r['card']['imtID'] for r in chosen}
            row = {'family': rr[0]['united_title'], 'family_key': family, 'chip': variant,
                   'black': [plain_card(r['card']) for r in black],
                   'expected_black': [{'code': r['external_code'], 'model': r['title']} for r in singles if color(r.get('color')) == 'K'],
                   'components': [{'color': color(r['ref'].get('color')), 'model': r['ref']['title'], 'card': plain_card(r['card']), 'expected_code': r['expected_code'], 'chip': r['chip']} for r in components],
                   'kits': [plain_card(r['card']) for r in kits], 'other_execution_kits': [plain_card(r['card']) for r in kit_records.values() if r not in kits],
                   'missing_colors': missing, 'missing_kit': not kit_records and not possible_sets, 'needs_kit_execution_review': bool(kit_records) and not kits,
                   'unverified_set_candidates': possible_sets if not kit_records else [],
                   'full_colors': set(COLORS) <= cols, 'all_found_together': len(roots) == 1,
                   'replacements': [{'expected_code': r['expected_code'], 'actual': plain_card(r['card'])} for r in chosen if r['card']['vendorCode'] != r['expected_code']],
                   'derivatives': [plain_card(c) for ref in singles for c in tails.get(ref['external_code'], [])], 'reasons': [], 'status': 'pending'}
            row['black_model_evidence'] = BLACK_RELATIONS.get(family)
            if family in MISSING_BLACK_RELATIONS and not row['black']:
                black_model, proof_url = MISSING_BLACK_RELATIONS[family]
                possible = bymodel.get(model_key(black_model), [])
                for miss in row['missing_colors']:
                    if miss['color'] == 'K':
                        miss['expected'] = [{'code': 'новый код не присвоен', 'model': black_model}]
                        miss['alternatives'] = [plain_card(c) for c in possible]
                        miss['status'] = 'possible_replacement' if possible else 'absent_active_catalog'
                row['expected_black'] = [{'code': 'новый код не присвоен', 'model': black_model}]
                row['black_model_evidence'] = (black_model, proof_url)
            special = ADJUDICATED.get(row['family'])
            if special:
                for miss in row['missing_colors']:
                    if not miss['expected'] and miss['color'] in special.get('missing', {}):
                        miss['expected'] = [{'code': 'новый код не присвоен', 'model': special['missing'][miss['color']]}]
                        miss['status'] = 'absent_active_catalog'
                if not row['expected_black'] and special.get('missing', {}).get('K'):
                    row['expected_black'] = [{'code': 'новый код не присвоен', 'model': special['missing']['K']}]
                if special.get('warning'): row['reasons'].append(special['warning'])
                row['family_identity_evidence'] = special['evidence']
            if variant == 'противоречие': row['reasons'].append('Есть явные противоположные сведения о чипе; сверить WB/ТК и наименования поставщиков, не смешивать исполнения')
            row['compatibility_details'] = [{'card': plain_card(r['card']), 'printers_wb': char(r['card'], 179078), 'printers_tc': [str(x.get('brand', '')) + ' ' + str(x.get('title', '')) if isinstance(x, dict) else str(x) for x in (r['ref'].get('raw') or {}).get('printer_models', [])] if isinstance((r['ref'].get('raw') or {}).get('printer_models'), list) else (r['ref'].get('raw') or {}).get('printer_models'), 'chip': r['chip'], 'chip_marks': r['evidence'], 'resource_wb': char(r['card'], 88963), 'resource_tc': r['ref'].get('resource')} for r in chosen]
            used.update(r['card']['nmID'] for r in components)
            if len(roots) == 1:
                row['status'] = 'complete' if row['full_colors'] and kits else 'available_together_incomplete'
            elif len(chosen) >= 2:
                op, reasons = make_operation(chosen, own, groups, family, variant, refs)
                ambiguous = False
                for co in COLORS:
                    same = [c for c in components if color(c['ref'].get('color')) == co]
                    if len(same) < 2: continue
                    aliases = [{model_key(p) for p in c['ref']['title'].split('/')} for c in same]
                    if len({number(char(c['card'], 88963)) for c in same}) > 1 or not set.intersection(*aliases): ambiguous = True
                if ambiguous:
                    reasons.append('Одному цвету соответствуют разные модели/ресурс; смешивание запрещено')
                    op = None
                row['reasons'] = reasons
                if op:
                    proposals.append(op)
                    row.update(status='ready', operation_id=op['id'])
                else:
                    row['status'] = 'needs_specific_review'
            else:
                row['status'] = 'only_one_available'
            rows.append(row)
    owners = defaultdict(set)
    black_owners = defaultdict(set)
    for row in rows:
        for c in row['black']: black_owners[c['nm_id']].add(row['family_key'])
    for row in rows:
        shared = [c for c in row['black'] if len(black_owners[c['nm_id']]) > 1]
        row['shared_black'] = shared
        if shared:
            row['reasons'].append('Чёрный используется несколькими модельными сериями: ' + ', '.join(c['code'] for c in shared) + '. Один SKU может иметь только одну группу WB; это не отсутствующая модель и не повод создавать дубликат.')
    for op in proposals:
        for c in op['before_cards']:
            owners[c['nmID']].add(op['id'])
    blocked = {op_id for ids in owners.values() if len(ids) > 1 for op_id in ids}
    for row in rows:
        if row.get('operation_id') in blocked:
            row['status'] = 'needs_specific_review'
            row['reasons'].append('Исходная группа участвует в нескольких семействах; нужен единый разбор её состава')
            row.pop('operation_id', None)
    covered_kits = {c['nm_id'] for row in rows for c in row['kits'] + row['other_execution_kits'] + row.get('unverified_set_candidates', [])}
    unassigned_sets = [{'card': plain_card(c), 'model': ref['title'], 'family': ref.get('united_title'), 'quantity_wb': quantities(c)[1], 'colors_tc': (ref.get('raw') or {}).get('colors_count'), 'reason': 'Не сопоставлен с действующими основными этой проверкой; не создавать дубликат набора'} for ref in all_kits for c in candidates(ref) if c['nmID'] not in covered_kits]
    excluded_ids = {r['card']['nm_id'] for r in excluded_technique}
    expected_color_ids = {c['nmID'] for ref in eligible if ref['consumable_type'] in regroup.CARTRIDGES and color(ref.get('color')) in {'C', 'M', 'Y'} for c in bycode.get(ref['external_code'], [])} - excluded_ids
    unresolved_ids = {r['card']['nm_id'] for r in no_family}
    uncovered = [{'card': plain_card(c), 'reason': 'Основная цветная не вошла в модельные группы: проверить модель, тип, цвет и количество'} for c in cards if c['nmID'] in expected_color_ids - used - unresolved_ids]
    return {'account': 'wb_acc1', 'prepared_at': now(), 'catalog_read_at': snapshot['read_at'], 'rows': rows,
            'unresolved_family': no_family, 'derived_families': derived_families, 'excluded_technique': excluded_technique, 'covered_color_parent_skus': len(used),
            'unassigned_sets': unassigned_sets, 'uncovered_color_cards': uncovered,
            'coverage': {'declared_color_main_skus': len(expected_color_ids), 'covered_declared_color_skus': len(expected_color_ids & used), 'without_family': len(expected_color_ids & unresolved_ids), 'other_uncovered': len(uncovered)},
            'operations': [op for op in proposals if op['id'] not in blocked]}


def prepare():
    if (OUT / 'apply_results.json').exists():
        raise RuntimeError('An executed plan is immutable; use --report or a new dated directory')
    p = build(json.loads((OUT / 'catalog.json').read_text()), json.loads((OUT / 'references.json').read_text()), json.loads((OUT / 'all_models.json').read_text()))
    regroup.save(OUT / 'plan.json', p)
    print('Census', len(p['rows']), 'variants;', len(p['operations']), 'safe operations;', len(p['unresolved_family']), 'cards without family', flush=True)


def apply():
    plan = json.loads((OUT / 'plan.json').read_text())
    path = OUT / 'apply_results.json'
    state = json.loads(path.read_text()) if path.exists() else {'results': [], 'started_at': now(), 'finished': False}
    locks = []
    for folder in [regroup.OUT, core.OUT, OUT]:
        handle = (folder / 'apply.lock').open('a'); fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB); locks.append(handle)
    old_out = regroup.OUT; regroup.OUT = OUT
    try:
        for original in plan['operations']:
            if any(r['id'] == original['id'] and r['status'] == 'confirmed' for r in state['results']):
                continue
            op = original
            if entries(OUT / 'journal.jsonl', op['id']):
                op, proof = make_fresh(original, Content(), entries(OUT / 'journal.jsonl', op['id']))
                regroup.save(OUT / f'restart_{op["id"]}.json', proof)
            for attempt in range(4):
                try:
                    result = regroup.execute(op, Content())
                    if result['status'] != 'confirmed':
                        raise RuntimeError('Live group changed; read independently before further writes')
                    result['confirmed_at'] = now(); state['results'].append(result)
                    state.pop('stopped_at', None); state.pop('reason', None); regroup.save(path, state)
                    print('CONFIRMED', len(state['results']), '/', len(plan['operations']), original['title'], flush=True)
                    break
                except Exception as exc:
                    state.update(stopped_at=original['id'], reason=str(exc)); regroup.save(path, state)
                    if attempt == 3 or not retryable(state, entries(OUT / 'journal.jsonl', op['id'])):
                        raise
                    time.sleep(10 * (attempt + 1))
                    op, proof = make_fresh(original, Content(), entries(OUT / 'journal.jsonl', op['id']))
                    regroup.save(OUT / f'recovery_{op["id"]}_{attempt}.json', proof)
        state.update(finished=True, finished_at=now()); regroup.save(path, state)
    finally:
        regroup.OUT = old_out


def report():
    snapshot = json.loads((OUT / 'catalog.json').read_text())
    source = json.loads((OUT / 'references.json').read_text())
    models = json.loads((OUT / 'all_models.json').read_text())
    state = json.loads((OUT / 'apply_results.json').read_text()) if (OUT / 'apply_results.json').exists() else {'results': []}
    # Additional analysis waves keep immutable original plans. Their confirmed
    # memberships are overlaid in execution order, without fetching history
    # again or pretending that a prepared plan was actually executed.
    for extra in sorted(OUT.glob('wave*/apply_results.json')):
        wave = json.loads(extra.read_text())
        state['results'].extend(r for r in wave['results'] if r['status'] == 'confirmed')
        if wave.get('finished_at'): state['finished_at'] = max(state.get('finished_at', ''), wave['finished_at'])
    lookup = {c['nmID']: c for c in snapshot['cards']}
    moved = set()
    for r in state['results']:
        if r['status'] != 'confirmed':
            continue
        for part in r['partitions']:
            for nm in part['nm_ids']:
                if lookup[nm]['imtID'] != part['root']:
                    moved.add(nm)
                lookup[nm]['imtID'] = part['root']
    census = build(snapshot, source, models)
    counts = Counter(r['status'] for r in census['rows'])
    summary = {'account': 'wb_acc1', 'catalog_read_at': snapshot['read_at'], 'confirmed_at': state.get('finished_at'),
               'catalog_skus': len(snapshot['cards']), 'model_families': len({r['family_key'] for r in census['rows']}),
               'family_variants': len(census['rows']), 'covered_color_parent_skus': census['covered_color_parent_skus'],
               'statuses': dict(counts), 'confirmed_operations': len(state['results']), 'moved_skus': len(moved),
               'missing_colors_cases': sum(bool(r['missing_colors']) for r in census['rows']),
               'missing_kit_cases': sum(r['missing_kit'] for r in census['rows']),
               'unresolved_family_cards': len(census['unresolved_family'])}
    summary['color_coverage'] = census['coverage']
    summary['unassigned_existing_sets'] = len(census['unassigned_sets'])
    new_sets = creation_rows(census)
    summary['sets_to_create'] = len(new_sets)
    summary['existing_set_contents_to_check'] = sum(bool(r.get('unverified_set_candidates')) for r in census['rows'])
    summary['set_chip_execution_to_check'] = sum(r['needs_kit_execution_review'] for r in census['rows'])
    regroup.save(OUT / 'census.json', census); regroup.save(OUT / 'summary.json', summary)
    render(census, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def card_link(c):
    stock = FBS.get(str(c['nm_id']))
    suffix = ' · FBS: ' + str(stock) if stock is not None else ' · FBS: нет данных'
    return '<a target="_blank" href="https://www.wildberries.ru/catalog/' + str(c['nm_id']) + '/detail.aspx">' + html.escape(c['code'] + ' · ' + c['title']) + '</a>' + suffix


def creation_rows(census):
    return [r for r in census['rows'] if r['missing_kit'] and r['full_colors'] and len({c['color'] for c in r['components']}) == len(r['components']) and r['chip'] != 'противоречие']


def render(census, summary):
    global FBS
    stock_data = json.loads((OUT / 'fbs.json').read_text()) if (OUT / 'fbs.json').exists() else {}
    FBS = stock_data.get('stocks', {})
    esc = lambda v: html.escape(str(v))
    statuses = {'complete': 'Все четыре цвета и набор вместе', 'available_together_incomplete': 'Имеющиеся товары вместе, но состав неполный',
                'needs_specific_review': 'Необходима конкретная проверка состава/данных', 'ready': 'Подготовлено к доклейке', 'only_one_available': 'Найдена только одна основная', 'primary_cards_absent': 'Основные отсутствуют, остались бандлы/дочерние'}
    body = '<h1>Цветные лазерные acc1 — весь ассортимент, включая нулевые остатки</h1><p>Источник: действующие карточки Content API, ' + esc(summary['catalog_read_at']) + '. Отбор по остатку не применялся. Отсутствие в продаже не означает удаление карточки. Подтверждённые изменения: ' + esc(summary['confirmed_at']) + '.</p>'
    body += '<p>Проверено ' + str(summary['model_families']) + ' семейств / ' + str(summary['family_variants']) + ' вариантов исполнения. Полностью собраны четыре цвета и набор: ' + str(summary['statuses'].get('complete', 0)) + '. Новых подтверждённых операций: ' + str(summary['confirmed_operations']) + ', фактически сменили группу ' + str(summary['moved_skus']) + ' SKU.</p>'
    body += '<p>FBS — остаток на складе продавца, проверка ' + esc(stock_data.get('at', 'не выполнена')) + '. Это не подтверждение доступности доставки в конкретный регион; FBO здесь не учтён. Нулевые остатки не исключались.</p><p><a href="#sets">Наборы на создание</a> · <a href="#absent">Конкретные отсутствующие модели</a> · <a href="#missing">Все причины неполного состава</a> · <a href="#review">Что исправить</a> · <a href="#all">Все семейства</a></p>'
    def black(r):
        value = '<br>'.join(card_link(c) for c in r['black']) or 'Основной чёрный не найден: ' + esc('; '.join(x['code'] + ' ' + x['model'] for x in r['expected_black']) or 'точная модель не сопоставлена')
        source = r.get('black_model_evidence')
        if source: value += '<br>' + ('<a href="' + esc(source[1]) + '">Модель чёрного подтверждена производителем</a>' if source[1].startswith('https://') else esc(source[1]))
        return value
    def missing(r):
        bits = []
        for m in r['missing_colors']:
            text = LABELS[m['color']] + ': '
            if m['status'] == 'different_execution_exists': text += 'есть в другом исполнении; не создавать дубликат'
            elif m['status'] == 'possible_replacement': text += 'есть похожее обозначение модели; проверить замену'
            elif m['status'] == 'absent_active_catalog': text += 'основная не найдена в действующем каталоге'
            else: text += 'точная модель не определена; отсутствие товара не доказано'
            text += ' / ' + '; '.join(x['code'] + ' ' + x['model'] for x in m['expected'])
            bits.append(esc(text) + '<br>' + '<br>'.join(card_link(c) for c in m['alternatives']))
        if r['missing_kit']:
            bits.append('Набор не найден по своему коду, точным моделям и составу. ' + ('Предлагаемый состав: ' + esc(' + '.join(c['model'] for c in r['components'])) if r['full_colors'] else 'Сначала определить/найти недостающие цвета; готовый состав нового набора не утверждён.'))
        if r['needs_kit_execution_review']:
            bits.append('Набор есть, но его исполнение требует проверки: ' + '<br>'.join(card_link(c) for c in r['other_execution_kits']))
        if r.get('unverified_set_candidates'):
            bits.append('Не создавать новый набор: существующая карточка уже имеет количество 4 или признаки набора. Подтвердить её фактический состав/тип: ' + '<br>'.join(card_link(c) for c in r['unverified_set_candidates']))
        return '<br>'.join(bits) or 'Все цвета и набор найдены'
    def row(r):
        components = '<br>'.join(esc(c['color'] + ' / ' + c['model']) + ' — ' + card_link(c['card']) for c in r['components'])
        derivative_note = '<details><summary>Оставшиеся бандлы/дочерние</summary>' + '<br>'.join(card_link(c) for c in r['derivatives']) + '</details>' if not r['components'] else ''
        proof = '<details><summary>Точные списки принтеров, ресурс и чипы для сверки</summary>' + '<br><br>'.join(card_link(x['card']) + '<br>Принтеры WB: ' + esc(x['printers_wb']) + '<br>Принтеры ТК: ' + esc(x['printers_tc']) + '<br>Ресурс WB/ТК: ' + esc(str(x['resource_wb']) + ' / ' + str(x['resource_tc'])) + '<br>Чип: ' + esc(x['chip']) + '; явные отметки: ' + esc(x['chip_marks']) for x in r.get('compatibility_details', [])) + '</details>'
        return '<tr><td>' + esc(r['family'] + ' / ' + r['chip']) + '<br>' + black(r) + '</td><td>' + components + '<br>' + '<br>'.join(card_link(c) for c in r['kits']) + derivative_note + '</td><td>' + missing(r) + '</td><td>' + esc(statuses[r['status']]) + '<br>' + '<br>'.join(esc(x) for x in r['reasons']) + proof + '</td></tr>'
    headers = '<tr><th>Семейство / чёрный родитель</th><th>Существующие цвета и наборы</th><th>Чего не хватает</th><th>Состояние / конкретное препятствие</th></tr>'
    new_sets = creation_rows(census)
    body += '<h2 id="sets">Наборы из четырёх цветов — кандидаты на создание: ' + str(len(new_sets)) + '</h2><p>Для каждого найдены все четыре основные. Проверены собственный артикул набора и наборы других артикулов с тем же составом и совместимостью. Новые товары не создавались; контентщику нужны состав, данные поставки и цена. Наборы с неустановленным исполнением перечислены отдельно, не включены сюда.</p><div class="scroll"><table>' + headers + ''.join(row(r) for r in new_sets) + '</table></div>'
    absent = [(r, m) for r in census['rows'] for m in r['missing_colors'] if m['status'] == 'absent_active_catalog']
    body += '<h2 id="absent">Точные модели, не найденные среди действующих основных — ' + str(len(absent)) + '</h2><p>Если основной чёрный также отсутствует, указан его ожидаемый код/модель. Отсутствие в каталоге не является разрешением восстановить намеренно удалённый товар. Для моделей, подтверждённых только OEM, сначала найти поставку и присвоить внешний код.</p><table><tr><th>Для какого чёрного / семейства</th><th>Недостающий цвет</th><th>Точная модель / ожидаемый код</th><th>Существующие компоненты</th></tr>'
    for r, m in absent:
        body += '<tr><td>' + black(r) + '<br>' + esc(r['family']) + '</td><td>' + LABELS[m['color']] + '</td><td>' + esc('; '.join(x['model'] + ' / ' + x['code'] for x in m['expected'])) + '</td><td>' + '<br>'.join(card_link(c['card']) for c in r['components']) + '</td></tr>'
    body += '</table>'
    for ident, title, chosen in [('missing', 'Отсутствующие цвета и наборы — по чёрному родителю', [r for r in census['rows'] if r['missing_colors'] or r['missing_kit'] or r.get('unverified_set_candidates')]),
                                 ('review', 'Конкретные действия: что исправить до следующей доклейки', [r for r in census['rows'] if r['status'] in {'ready', 'needs_specific_review'} or r.get('unverified_set_candidates') or r['needs_kit_execution_review'] or r['chip'] == 'противоречие']),
                                 ('all', 'Все проверенные семейства', census['rows'])]:
        body += '<h2 id="' + ident + '">' + title + ' — ' + str(len(chosen)) + '</h2><div class="scroll"><table>' + headers + ''.join(row(r) for r in chosen) + '</table></div>'
    body += '<h2>Цветные основные без семейства в справочнике</h2>' + '<br>'.join(card_link(r['card']) + ' — ' + esc(r['reason']) for r in census['unresolved_family'])
    body += '<h2>Существующие наборы вне сопоставленных семейств — ' + str(len(census['unassigned_sets'])) + '</h2>' + '<br>'.join(card_link(r['card']) + ' — ' + esc(r['reason']) + '; состав модели: ' + esc(r['model']) + '; количество WB: ' + esc(r['quantity_wb']) for r in census['unassigned_sets'])
    coverage = census['coverage']
    body += '<h2>Контроль охвата цветных основных</h2><p>В каталоге ' + str(coverage['declared_color_main_skus']) + ' основных C/M/Y, отнесённых источником к лазерным после исключения подтверждённых ошибок типа. В семейства включены ' + str(coverage['covered_declared_color_skus']) + '; без подтверждённого семейства ' + str(coverage['without_family']) + '; остальные несопоставленные ' + str(coverage['other_uncovered']) + '. Чёрные и наборы считаются отдельно; это не количество всех SKU каталога.</p>' + '<br>'.join(card_link(r['card']) + ' — ' + esc(r['reason']) for r in census['uncovered_color_cards'])
    audit_file = OUT / 'replacement_checks.json'
    if audit_file.exists():
        audit = json.loads(audit_file.read_text())
        body += '<h2 id="replacements">Проверка прежних десяти отсутствующих основных</h2><p>Поиск выполнен по всему действующему каталогу: свой внешний код, точная модель, похожее написание модели, дочерние/бандлы. Проверена свежая корзина WB. Наличие другого ресурса не выдаётся за точную замену.</p><table><tr><th>Ожидаемый код / модель</th><th>Результат</th></tr>'
        for x in audit['rows']:
            body += '<tr><td>' + esc(x['code'] + ' / ' + x['model']) + '</td><td>' + esc(x['conclusion']) + '<br>' + '<br>'.join(card_link(c) for c in x['alternatives']) + '</td></tr>'
        body += '</table>'
    body += '<h2>Исключены: чернильные и ошибочно названные картриджами барабаны/блоки</h2>' + '<br>'.join(card_link(r['card']) + ' — ' + esc(r['reason']) + (' <a href="' + esc(r['source']) + '">Производитель</a>' if r['source'] else ' — источник: наименования товаров поставщиков МойСклада') for r in census['excluded_technique'])
    document = '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Цветные лазерные acc1</title><style>body{font:16px system-ui;line-height:1.5;max-width:1500px;margin:24px auto;padding:16px;background:#fafaf7;color:#182721}a{color:#14615c}table{border-collapse:collapse;width:100%}td,th{border:1px solid #bac8bf;padding:9px;vertical-align:top;text-align:left}.scroll{overflow:auto}</style>' + body + '</html>'
    (OUT / 'index.html').write_text(document)
    with (OUT / 'missing.csv').open('w', encoding='utf-8-sig', newline='') as f:
        labels = {'model_unmapped': 'Точная модель не определена, отсутствие товара не доказано', 'absent_active_catalog': 'Основная не найдена в действующем каталоге', 'different_execution_exists': 'Цвет есть в другом исполнении, не создавать дубликат', 'possible_replacement': 'Найдена похожая модель: проверить замену', 'kit_missing': 'Набор не найден'}
        w = csv.writer(f, delimiter=';'); w.writerow(['Семейство', 'Чип', 'Чёрный артикул WB', 'Чёрный внешний код (имеющийся или ожидаемый)', 'Недостающий цвет', 'Ожидаемые модели', 'Что делать', 'Набор отсутствует'])
        for r in census['rows']:
            for m in r['missing_colors'] or ([{'color': '', 'expected': [], 'status': 'kit_missing'}] if r['missing_kit'] else []):
                w.writerow([r['family'], r['chip'], ', '.join(str(c['nm_id']) for c in r['black']), ', '.join(c['code'] for c in r['black'] or r['expected_black']), LABELS.get(m['color'], ''), ', '.join(x['code'] + ' ' + x['model'] for x in m['expected']), labels[m['status']], 'да' if r['missing_kit'] else 'нет'])
    with (OUT / 'sets_to_create.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter=';'); w.writerow(['Семейство', 'Исполнение чипа', 'Чёрный родитель — наш код', 'Чёрный родитель — WB', 'Ссылка на чёрный', 'Состав набора: внешние коды / модели / цвета', 'Карточки компонентов WB'])
        for r in new_sets:
            w.writerow([r['family'], r['chip'], ', '.join(c['code'] for c in r['black']), ', '.join(str(c['nm_id']) for c in r['black']), ', '.join(c['url'] for c in r['black']), '; '.join(c['card']['code'] + ' / ' + c['model'] + ' / ' + LABELS[c['color']] for c in r['components']), '; '.join(c['card']['url'] for c in r['components'])])
    with (OUT / 'absent_models.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter=';'); w.writerow(['Семейство', 'Для какого чёрного (имеющийся или ожидаемый код / модель)', 'Ссылка на имеющийся чёрный', 'Недостающий цвет', 'Точная модель / ожидаемый код', 'Имеющиеся компоненты WB', 'Действие'])
        for r, m in absent:
            target = ', '.join(c['code'] + ' / ' + c['title'] for c in r['black']) or ', '.join(c['code'] + ' / ' + c['model'] for c in r['expected_black'])
            w.writerow([r['family'], target, ', '.join(c['url'] for c in r['black']), LABELS[m['color']], '; '.join(x['model'] + ' / ' + x['code'] for x in m['expected']), '; '.join(c['card']['url'] for c in r['components']), 'Проверить намеренное удаление/наличие поставщика перед заведением. Автоматически не создавать.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--out', type=Path); parser.add_argument('--collect', action='store_true'); parser.add_argument('--prepare', action='store_true'); parser.add_argument('--apply', action='store_true'); parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.out:
        if not args.out.resolve().is_relative_to(regroup.ROOT / 'docs/reports'): raise RuntimeError('Output must stay under project reports')
        OUT = args.out.resolve()
    if args.collect: collect()
    if args.prepare: prepare()
    if args.apply: apply()
    if args.report: report()

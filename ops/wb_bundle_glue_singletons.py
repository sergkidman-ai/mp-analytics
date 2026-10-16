#!/usr/bin/env python3
# поток: mkt
"""Prepare/apply acc1 bundles to ungrouped, single cartridge parents only.

Explicitly authorized 07.10.2026 by Sergey. No price, description, stock or ads
writes. No silent-supplier -> no-chip inference. Unknown chip is inherited only
inside an otherwise verified identical parent family. Every move is pre-read,
journaled, and verified before the next one. Uncertain writes are never retried.
"""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

import requests

from collectors.wb import CARDS_URL, _content_token
from core.db import query
from ops.wb_bundle_plan import char, fingerprint, is_bundle, normalized, number, quantities
from ops.wb_supplier_chip_evidence import recognize

ROOT = Path(__file__).resolve().parents[1]
MOVE = 'https://content-api.wildberries.ru/content/v2/cards/moveNm'
ACCOUNT = 'wb_acc1'
SINGLE_TYPES = {'Картридж', 'Тонер-картридж'}
DATE = '2026-10-07'
OUT = ROOT / f'docs/reports/mkt_wb_bundle_glue_singletons_{DATE}_acc1'


def now():
    return datetime.now(timezone.utc).isoformat()


def compatible_values(card, ident):
    value = next((x.get('value') for x in card.get('characteristics', []) if x.get('id') == ident), [])
    if not isinstance(value, list):
        value = [value]
    return tuple(sorted(normalized(v) for v in value if v is not None and str(v).strip()))


def explicit_chip(text, supplier=''):
    return set(recognize(text or '', supplier)['classes'])


def family_reasons(parent, bundles, reference, supplier_marks):
    """No missing-chip blocker; contradictions and mismatched products block."""
    reasons = set()
    if reference.get('consumable_type') not in SINGLE_TYPES:
        reasons.add('Основная не является одиночным картриджем по ТК')
    if number(char(parent, 179792)) != 1:
        reasons.add('Количество основной карточки не равно 1')
    # A single ink cartridge can contain multiple colors. colors_count is not
    # the number of physical cartridges and cannot identify a compound pack.
    if re.search(r'\b([2-9]|\d{2,})\s*шт|\b[xх×]\s*[2-9]\b', parent.get('title', ''), re.I):
        reasons.add('В названии основной указана составная упаковка')
    if not char(parent, 5023) or normalized(char(parent, 5023)) != normalized(reference.get('title', '')):
        reasons.add('Модель основной не подтверждена ТК')
    if not compatible_values(parent, 179078):
        reasons.add('Совместимость основной не заполнена')
    if len(supplier_marks) > 1:
        reasons.add('Разные явные классы чипа у поставщиков одного родителя')
    declared = set(supplier_marks)
    tc_chip = {'chip': 'с чипом', 'nochip': 'без чипа', 'chip_free': 'чип без счётчика'}.get(reference.get('chip'))
    if tc_chip:
        declared.add(tc_chip)
    if len(declared) > 1:
        reasons.add('Явное указание поставщика расходится с ТК по чипу')
    all_wb_marks = set()
    for c in [parent, *bundles]:
        all_wb_marks |= explicit_chip(char(c, 378533))
        all_wb_marks |= explicit_chip(c.get('description', ''))
    if len(all_wb_marks) > 1:
        reasons.add('У основной и бандлов разные явные указания чипа ВБ')
    if declared and all_wb_marks and declared != all_wb_marks:
        reasons.add('Явное указание чипа в ВБ расходится с источниками')
    for b in bundles:
        if b.get('subjectID') != parent.get('subjectID'):
            reasons.add('Разные предметы ВБ')
        qty, field, title, code = quantities(b)
        if code is None or code <= 1 or any(x is not None and x != code for x in (field, title)):
            reasons.add('Количество бандла расходится между кодом, названием и полем')
        if not re.match(r'^' + re.escape(parent['vendorCode']) + r'[XХ]\d+', b.get('vendorCode', ''), re.I):
            reasons.add('Бандл относится к другому родительскому коду')
        for ident, label in ((5023, 'модель'), (58854, 'цвет'), (88963, 'ресурс')):
            if normalized(char(b, ident)) != normalized(char(parent, ident)):
                reasons.add(f'Бандл отличается от основной: {label}')
        if compatible_values(b, 179078) != compatible_values(parent, 179078):
            reasons.add('Список совместимых принтеров отличается')
    return sorted(reasons)


def read_references(codes):
    refs = {r['external_code']: r for r in query(
        'select external_code,title,consumable_type,color,resource,chip,raw from prc_tc_model '
        'where gone_at is null and external_code=any(%s)', (list(codes),))}
    marks = defaultdict(set)
    for r in query('select external_code,name from ms_product where external_code=any(%s)', (list(codes),)):
        marks[r['external_code']] |= explicit_chip(r['name'])
    prices = query('''select distinct m.external_code, p.name, l.supplier_key
        from prc_price_row p join ms_product m on m.ms_id=p.ms_id
        join prc_price_load l on l.id=p.load_id where m.external_code=any(%s)''', (list(codes),))
    for r in prices:
        marks[r['external_code']] |= explicit_chip(r['name'], r['supplier_key'] or '')
    return refs, marks


def build_plan(snapshot, references, chip_marks):
    cards = snapshot['cards']
    groups, parents, bundles = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in cards:
        groups[c['imtID']].append(c)
        if re.fullmatch(r'\d{4}', c.get('vendorCode', '')):
            parents[c['vendorCode']].append(c)
        if is_bundle(c):
            bundles[c['vendorCode'][:4]].append(c)
    operations, review = [], []
    for base, pp in sorted(parents.items()):
        ref = references.get(base, {})
        if not bundles[base] or ref.get('consumable_type') not in SINGLE_TYPES:
            continue
        if len(pp) != 1:
            review.append({'base': base, 'parent_nm': '', 'bundles': len(bundles[base]), 'reasons': ['Несколько основных карточек']})
            continue
        p = pp[0]
        if len(groups[p['imtID']]) != 1 or number(char(p, 179792)) != 1:
            continue
        bb = sorted(bundles[base], key=lambda c: c['nmID'])
        reasons = family_reasons(p, bb, ref, chip_marks.get(base, set()))
        own = {p['nmID'], *(c['nmID'] for c in bb)}
        for b in bb:
            if any(c['nmID'] not in own for c in groups[b['imtID']]):
                reasons.append('В исходной группе бандла есть чужие позиции')
        if len(own) > 30:
            reasons.append('Больше 30 карточек после объединения')
        if reasons:
            review.append({'base': base, 'parent_nm': p['nmID'], 'bundles': len(bb), 'reasons': sorted(set(reasons))})
            continue
        affected = [p, *bb]
        operations.append({'account': ACCOUNT, 'base': base, 'parent_nm': p['nmID'],
            'parent_title': p.get('title', ''), 'target_imt': p['imtID'],
            'nm_ids': [b['nmID'] for b in bb], 'quantities': [quantities(b)[3] for b in bb],
            'before_fingerprint': fingerprint(affected), 'before_cards': affected,
            'before_groups': {str(imt): [c['nmID'] for c in groups[imt]] for imt in {c['imtID'] for c in affected}},
            'chip': ', '.join(sorted(chip_marks.get(base, set()))) or 'В поставщиках не оговорён; наследование от того же родителя',
            'resource_note': '' if char(p, 88963) else 'Ресурс ВБ не указан у всей семьи; модель и совместимость совпадают',
            'colors_note': ('В ТК несколько цветов; физическое количество основной = 1, '
                            'тип одиночный картридж, вся семья совпадает по модели и цвету')
                           if (number((ref.get('raw') or {}).get('colors_count', 1)) or 1)>1 else '',
            'body': {'targetIMT': p['imtID'], 'nmIDs': [b['nmID'] for b in bb]},
            'rollback': [{'nmIDs': [b['nmID']]} for b in bb]})
    return {'account': ACCOUNT, 'prepared_at': now(), 'catalog_read_at': snapshot['read_at'],
        'scope': 'Одиночные картриджи acc1, родитель ни с кем не склеен',
        'operations': operations, 'review': review, 'marketplace_writes': 0}


def check_before(op, groups):
    """Reject any changed membership or product fields, including new foreign cards."""
    target = groups.get(str(op['target_imt']), [])
    if {c['nmID'] for c in target} != {op['parent_nm']}:
        return False, 'Основная уже не одиночная либо изменила группу'
    all_cards = {}
    for imt, expected in op['before_groups'].items():
        current = groups.get(imt, [])
        if {c['nmID'] for c in current} != set(expected):
            return False, 'Изменился состав исходной группы'
        all_cards.update({c['nmID']: c for c in current})
    if fingerprint(list(all_cards.values())) != op['before_fingerprint']:
        return False, 'После подготовки изменились поля товара'
    return True, ''


def validate_operation(op):
    """A saved preview cannot accidentally send a different body than checked."""
    if op.get('account') != ACCOUNT:
        raise RuntimeError('Operation belongs to a different account')
    ids=op['nm_ids']
    if not ids or len(ids)>29 or len(set(ids))!=len(ids) or op['parent_nm'] in ids:
        raise RuntimeError('Invalid moved card IDs')
    if op['body'] != {'targetIMT':op['target_imt'],'nmIDs':ids}:
        raise RuntimeError('Request body differs from verified operation')
    if op['before_groups'].get(str(op['target_imt'])) != [op['parent_nm']]:
        raise RuntimeError('Prepared target was not a singleton')
    if {c['nmID'] for c in op['before_cards']} != {op['parent_nm'], *ids}:
        raise RuntimeError('Incomplete before snapshot')
    if fingerprint(op['before_cards']) != op['before_fingerprint']:
        raise RuntimeError('Before snapshot fingerprint mismatch')


def content_fingerprint(cards):
    return fingerprint([{**c,'imtID':0} for c in cards])


class Content:
    def __init__(self):
        self.headers = {'Authorization': _content_token(ACCOUNT), 'Content-Type': 'application/json'}
        self.session = requests.Session()
        self.last_call = 0

    def request(self, url, body):
        # Shared content category limit: 100/min, 600ms. Leave room for other collectors.
        time.sleep(max(0, 0.85 - (time.monotonic() - self.last_call)))
        self.last_call = time.monotonic()
        return self.session.post(url, headers=self.headers, json=body, timeout=45)

    def group(self, imt):
        body = {'settings': {'sort': {'ascending': True}, 'cursor': {'limit': 100},
                            'filter': {'withPhoto': -1, 'imtID': int(imt)}}}
        for attempt in range(5):
            r = self.request(CARDS_URL, body)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(45, max(3, int(r.headers.get('Retry-After', '5')))))
                continue
            r.raise_for_status()
            data = r.json()
            if data.get('error'):
                raise RuntimeError('Content read returned error')
            cc = data.get('cards')
            if not isinstance(cc, list):
                raise RuntimeError('imtID filter did not return a complete, matching group')
            if len(cc) >= 100 or any(c.get('imtID') != int(imt) for c in cc):
                # WB can update the search index before updating card payloads
                # after moveNm. Retry reads, never the already accepted write.
                time.sleep(1 + attempt)
                continue
            return cc
        raise RuntimeError('Content group read retries exhausted')

    def move(self, body):
        # NEVER retry a write: a timeout or 5xx can still mean it was accepted.
        return self.request(MOVE, body)


def append_journal(entry):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT/'journal.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': now(), **entry}, ensure_ascii=False) + '\n')
        f.flush()


def apply_plan(plan, client):
    results = []
    total = len(plan['operations'])
    for i, op in enumerate(plan['operations'], 1):
        validate_operation(op)
        groups = {imt: client.group(int(imt)) for imt in op['before_groups']}
        desired = {op['parent_nm'], *op['nm_ids']}
        if {c['nmID'] for c in groups.get(str(op['target_imt']), [])} == desired:
            result = {'base': op['base'], 'parent_nm': op['parent_nm'], 'status': 'already_done', 'bundles': len(op['nm_ids'])}
            results.append(result); append_journal(result)
            continue
        valid, reason = check_before(op, groups)
        if not valid:
            result = {'base': op['base'], 'parent_nm': op['parent_nm'], 'status': 'skipped_changed', 'reason': reason, 'bundles': len(op['nm_ids'])}
            results.append(result); append_journal(result)
            continue
        append_journal({'base': op['base'], 'status': 'attempt', 'body': op['body'], 'before_groups': op['before_groups']})
        try:
            response = client.move(op['body'])
            append_journal({'base': op['base'], 'status': 'response', 'http': response.status_code, 'response': response.text[:1000]})
            response.raise_for_status()
            if response.content and response.json().get('error'):
                raise RuntimeError('WB rejected move')
        except Exception as exc:
            append_journal({'base': op['base'], 'status': 'write_uncertain_or_rejected', 'error_type': type(exc).__name__})
            (OUT/'apply_results.json').write_text(json.dumps({'finished': False, 'results': results, 'stopped_at': op['base'], 'reason': 'Запись не подтверждена; не повторять вслепую'}, ensure_ascii=False, indent=2))
            raise
        verified = False
        for delay in (0, 2, 4, 8, 12, 20):
            time.sleep(delay)
            after = client.group(op['target_imt'])
            members = {c['nmID'] for c in after}
            if members == desired and content_fingerprint(after) == content_fingerprint(op['before_cards']):
                verified = True
                break
            if members - desired:
                break
        if not verified:
            append_journal({'base': op['base'], 'status': 'verification_failed', 'observed_ids': sorted(members), 'expected_ids': sorted(desired)})
            (OUT/'apply_results.json').write_text(json.dumps({'finished': False, 'results': results, 'stopped_at': op['base'], 'reason': 'Итоговая группа не подтверждена'}, ensure_ascii=False, indent=2))
            raise RuntimeError('Move result not confirmed; stopped before further writes')
        result = {'base': op['base'], 'parent_nm': op['parent_nm'], 'status': 'confirmed', 'bundles': len(op['nm_ids']),
                  'target_imt': op['target_imt'], 'group_size': len(after), 'url': f"https://www.wildberries.ru/catalog/{op['parent_nm']}/detail.aspx"}
        results.append(result); append_journal(result)
        (OUT/'apply_results.json').write_text(json.dumps({'finished': False, 'results': results}, ensure_ascii=False))
        if i == 1 or i % 10 == 0 or i == total:
            print(f'Progress {i}/{total}; confirmed {sum(r["status"] == "confirmed" for r in results)} families', flush=True)
    result = {'finished': True, 'finished_at': now(), 'results': results,
              'status_counts': dict(Counter(r['status'] for r in results)),
              'bundles_moved': sum(r['bundles'] for r in results if r['status'] == 'confirmed')}
    (OUT/'apply_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != 'results'}, ensure_ascii=False), flush=True)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', type=Path)
    p.add_argument('--plan', type=Path)
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.apply:
        if not a.plan: p.error('--apply requires the saved --plan')
        plan = json.loads(a.plan.read_text())
        if plan['account'] != ACCOUNT or not plan['operations']: raise RuntimeError('Invalid/empty acc1 plan')
        apply_plan(plan, Content())
        return
    if not a.snapshot: p.error('Preparation requires --snapshot')
    snapshot=json.loads(a.snapshot.read_text())
    if snapshot['account'] != ACCOUNT: raise RuntimeError('Wrong snapshot account')
    codes={c['vendorCode'][:4] for c in snapshot['cards'] if is_bundle(c)}
    refs, marks=read_references(codes)
    plan=build_plan(snapshot, refs, marks)
    (OUT/'plan.json').write_text(json.dumps(plan, ensure_ascii=False))
    (OUT/'review.json').write_text(json.dumps(plan['review'], ensure_ascii=False, indent=2))
    records=[{'родительский_код':op['base'],'основная_nm_id':op['parent_nm'],'название':op['parent_title'],
              'ссылка':f"https://www.wildberries.ru/catalog/{op['parent_nm']}/detail.aspx",
              'бандлы_nm_id':'; '.join(map(str,op['nm_ids'])),'количества':'; '.join(map(str,op['quantities'])),
              'чип':op['chip'],'примечание_ресурс':op['resource_note']} for op in plan['operations']]
    if records:
        with (OUT/'families.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(records[0]),delimiter=';');w.writeheader();w.writerows(records)
    print(json.dumps({'families':len(plan['operations']), 'bundles':sum(len(op['nm_ids']) for op in plan['operations']),
        'review_families':len(plan['review']), 'review_reasons':dict(Counter(x for r in plan['review'] for x in r['reasons'])),
        'resource_unset_families':sum(bool(op['resource_note']) for op in plan['operations']), 'marketplace_writes':0},ensure_ascii=False))


if __name__ == '__main__': main()

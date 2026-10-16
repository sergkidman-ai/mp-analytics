#!/usr/bin/env python3
# поток: mkt
"""Acc1 laser bundles: extend existing groups, preserving every original member.

Preparation reads catalog snapshots and reference data. --apply executes only
the saved verified plan under Sergey's explicit laser-bundle instruction.
No prices, descriptions, advertising or database rows are written.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import time

from ops.wb_bundle_plan import fingerprint, is_bundle, quantities
from ops.wb_bundle_glue_singletons import (
    ACCOUNT, Content, SINGLE_TYPES, content_fingerprint, family_reasons,
    now, read_references,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/mkt_wb_bundle_glue_laser_groups_2026-10-07_acc1'


def laser(ref):
    return (ref.get('consumable_type') in SINGLE_TYPES and
            (ref.get('raw') or {}).get('assignment') == 'для лазерного принтера')


def build_plan(snapshot, references, marks):
    groups, parents, bundles = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in snapshot['cards']:
        groups[c['imtID']].append(c)
        if re.fullmatch(r'\d{4}', c.get('vendorCode', '')):
            parents[c['vendorCode']].append(c)
        if is_bundle(c):
            bundles[c['vendorCode'][:4]].append(c)
    candidates, review = defaultdict(list), []
    for base, bb in sorted(bundles.items()):
        ref = references.get(base, {})
        if not laser(ref):
            continue
        pp = parents.get(base, [])
        if len(pp) != 1:
            review.append({'base': base, 'bundles': len(bb), 'reason': 'Точная основная карточка не найдена либо неоднозначна'})
            continue
        p = pp[0]
        moving = sorted((b for b in bb if b['imtID'] != p['imtID']), key=lambda c: c['nmID'])
        if not moving:
            continue
        reasons = family_reasons(p, bb, ref, marks.get(base, set()))
        if reasons:
            review.append({'base': base, 'parent_nm': p['nmID'], 'bundles': len(moving), 'reason': '; '.join(reasons)})
            continue
        candidates[p['imtID']].append({'base': base, 'parent': p, 'moving': moving,
            'chip': ', '.join(sorted(marks.get(base, set()))) or 'Чип не оговорён: наследование от того же родителя'})

    operations = []
    for target, families in sorted(candidates.items()):
        # Remove entire families, never individual pack quantities. A source
        # group may move intact only when all members belong to approved families
        # going to this same target. Do not dismantle foreign existing groups.
        while families:
            moved = {c['nmID'] for f in families for c in f['moving']}
            rejected = []
            for f in families:
                if any(any(c['nmID'] not in moved for c in groups[b['imtID']]) for b in f['moving']):
                    rejected.append(f)
            if not rejected:
                break
            bad = {f['base'] for f in rejected}
            for f in rejected:
                review.append({'base': f['base'], 'parent_nm': f['parent']['nmID'],
                    'bundles': len(f['moving']), 'reason': 'В исходной группе бандла есть чужие позиции'})
            families = [f for f in families if f['base'] not in bad]
        if not families:
            continue
        moving = sorted((c for f in families for c in f['moving']), key=lambda c: c['nmID'])
        if len(groups[target]) + len(moving) > 30:
            for f in families:
                review.append({'base': f['base'], 'parent_nm': f['parent']['nmID'],
                    'bundles': len(f['moving']), 'reason': 'Вся группа превысит 30 карточек; частичный перенос не выбран'})
            continue
        affected_groups = {target, *(c['imtID'] for c in moving)}
        before = sorted((c for imt in affected_groups for c in groups[imt]), key=lambda c: c['nmID'])
        operations.append({'account': ACCOUNT, 'target_imt': target,
            'families': [{'base': f['base'], 'parent_nm': f['parent']['nmID'],
                         'parent_title': f['parent']['title'], 'nm_ids': [c['nmID'] for c in f['moving']],
                         'chip': f['chip']} for f in families],
            'nm_ids': [c['nmID'] for c in moving],
            'before_groups': {str(imt): sorted(c['nmID'] for c in groups[imt]) for imt in affected_groups},
            'before_cards': before, 'before_fingerprint': fingerprint(before),
            'expected_ids': sorted({c['nmID'] for c in groups[target]} | {c['nmID'] for c in moving}),
            'body': {'targetIMT': target, 'nmIDs': [c['nmID'] for c in moving]}})
    return {'account': ACCOUNT, 'scope': 'Лазерные одиночные картриджи acc1 и их собственные бандлы, включая существующие группы',
            'prepared_at': now(), 'catalog_read_at': snapshot['read_at'],
            'operations': operations, 'review': review, 'marketplace_writes': 0}


def validate(op):
    if op.get('account') != ACCOUNT:
        raise RuntimeError('Wrong account')
    moved = op['nm_ids']
    target = op['target_imt']
    if not moved or len(moved) != len(set(moved)):
        raise RuntimeError('Empty or duplicated moved IDs')
    if op['body'] != {'targetIMT': target, 'nmIDs': moved}:
        raise RuntimeError('Request differs from verified operation')
    core = set(op['before_groups'][str(target)])
    if not core or core & set(moved) or set(op['expected_ids']) != core | set(moved) or len(op['expected_ids']) > 30:
        raise RuntimeError('Invalid target preservation or group limit')
    family_ids = [nm for f in op['families'] for nm in f['nm_ids']]
    if set(family_ids) != set(moved) or len(family_ids) != len(moved):
        raise RuntimeError('Moved IDs differ from verified families')
    if any(f['parent_nm'] not in core for f in op['families']):
        raise RuntimeError('Parent is outside original target')
    before_ids = {c['nmID'] for c in op['before_cards']}
    if before_ids != core | set(moved) or fingerprint(op['before_cards']) != op['before_fingerprint']:
        raise RuntimeError('Before snapshot is incomplete or changed')
    for imt, members in op['before_groups'].items():
        if set(members) != {c['nmID'] for c in op['before_cards'] if c['imtID'] == int(imt)}:
            raise RuntimeError('Source snapshot membership mismatch')
        if int(imt) != target and not set(members).issubset(set(moved)):
            raise RuntimeError('Foreign source cards would be separated')


def check_before(op, groups):
    for imt, expected in op['before_groups'].items():
        if {c['nmID'] for c in groups[imt]} != set(expected):
            return False, 'Изменился состав группы после подготовки'
    cards = [c for cc in groups.values() for c in cc]
    if fingerprint(cards) != op['before_fingerprint']:
        return False, 'Изменились поля карточек после подготовки'
    return True, ''


def append_journal(entry):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT/'journal.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': now(), **entry}, ensure_ascii=False) + '\n')
        f.flush()


def checkpoint(results, finished=False, **extra):
    state = {'finished': finished, 'results': results, **extra}
    tmp = OUT/'apply_results.tmp'
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(OUT/'apply_results.json')


def apply_plan(plan, client, resume=False):
    op_targets = {op['target_imt'] for op in plan['operations']}
    prior = {}
    state_path = OUT/'apply_results.json'
    if resume and state_path.exists():
        prior = {r['target_imt']: r for r in json.loads(state_path.read_text()).get('results', [])
                 if r['target_imt'] in op_targets}
    results = list(prior.values())
    for i, op in enumerate(plan['operations'], 1):
        validate(op)
        if op['target_imt'] in prior:
            continue
        target = client.group(op['target_imt'])
        expected = set(op['expected_ids'])
        basic = {'target_imt': op['target_imt'], 'families': len(op['families']),
                 'bundles': len(op['nm_ids']), 'bases': [f['base'] for f in op['families']]}
        if {c['nmID'] for c in target} == expected and content_fingerprint(target) == content_fingerprint(op['before_cards']):
            row = {**basic, 'status': 'already_done', 'group_size': len(target)}
            results.append(row); append_journal(row); checkpoint(results)
            continue
        groups = {str(op['target_imt']): target}
        for imt in op['before_groups']:
            if imt not in groups:
                groups[imt] = client.group(int(imt))
        ok, reason = check_before(op, groups)
        if not ok:
            row = {**basic, 'status': 'skipped_changed', 'reason': reason}
            results.append(row); append_journal(row); checkpoint(results)
            continue
        append_journal({**basic, 'status': 'attempt', 'body': op['body']})
        try:
            response = client.move(op['body'])
            append_journal({**basic, 'status': 'response', 'http': response.status_code, 'response': response.text[:1000]})
            response.raise_for_status()
            if response.content and response.json().get('error'):
                raise RuntimeError('WB rejected move')
        except Exception as exc:
            append_journal({**basic, 'status': 'write_uncertain_or_rejected', 'error_type': type(exc).__name__})
            checkpoint(results, stopped_at=op['target_imt'], reason='Запись не подтверждена; сначала чтение, не повторять вслепую')
            raise
        try:
            verified = False
            for delay in (0, 2, 4, 8, 12, 20):
                time.sleep(delay)
                after = client.group(op['target_imt'])
                members = {c['nmID'] for c in after}
                if members == expected and content_fingerprint(after) == content_fingerprint(op['before_cards']):
                    verified = True
                    break
                if members - expected:
                    break
            if not verified:
                raise RuntimeError('Target members or fields not confirmed')
        except Exception as exc:
            append_journal({**basic, 'status': 'verification_uncertain', 'error_type': type(exc).__name__})
            checkpoint(results, stopped_at=op['target_imt'], reason='Результат записи не подтверждён; сначала сверить группу чтением')
            raise
        row = {**basic, 'status': 'confirmed', 'group_size': len(after)}
        results.append(row); append_journal(row); checkpoint(results)
        if i == 1 or i % 10 == 0 or i == len(plan['operations']):
            print(f'Confirmed groups {i}/{len(plan["operations"])}; bundles {sum(r["bundles"] for r in results if r["status"] in ("confirmed","already_done"))}', flush=True)
    counts = dict(Counter(r['status'] for r in results))
    checkpoint(results, finished=True, finished_at=now(), status_counts=counts)
    print(json.dumps({'finished': True, 'status_counts': counts}, ensure_ascii=False), flush=True)
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', type=Path)
    p.add_argument('--plan', type=Path)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--resume', action='store_true', help='сохранить уже подтверждённые операции')
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.apply:
        if not a.plan:
            p.error('--apply requires saved --plan')
        plan = json.loads(a.plan.read_text())
        if plan.get('account') != ACCOUNT or not plan['operations']:
            raise RuntimeError('Invalid plan')
        apply_plan(plan, Content(), resume=a.resume)
        return
    if not a.snapshot:
        p.error('Preparation requires --snapshot')
    snapshot = json.loads(a.snapshot.read_text())
    if snapshot.get('account') != ACCOUNT:
        raise RuntimeError('Wrong catalog account')
    codes = {c['vendorCode'][:4] for c in snapshot['cards'] if is_bundle(c)}
    refs, marks = read_references(codes)
    plan = build_plan(snapshot, refs, marks)
    (OUT/'plan.json').write_text(json.dumps(plan, ensure_ascii=False))
    (OUT/'review.json').write_text(json.dumps(plan['review'], ensure_ascii=False, indent=2))
    print(json.dumps({'groups': len(plan['operations']),
        'families': sum(len(o['families']) for o in plan['operations']),
        'bundles': sum(len(o['nm_ids']) for o in plan['operations']),
        'review_families': len(plan['review']),
        'review_reasons': dict(Counter(r['reason'] for r in plan['review'])),
        'marketplace_writes': 0}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

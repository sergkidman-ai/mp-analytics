#!/usr/bin/env python3
# поток: card
"""One-night authorized Rapid description refresh; supplier stock is never a filter."""
import argparse
import csv
import fcntl
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from invoice_bot.supplier_groups import ID2GROUP

MSK = ZoneInfo('Europe/Moscow')
IDS = {sid for sid, group in ID2GROUP.items() if group == 'Суперфайн'}
BASE_OUT = ROOT / 'docs/reports/card_rapid_description_sync_2026-10-08'
CAP, BATCH, PAUSE = 4000, 50, 60


def stamp():
    return datetime.now(MSK).isoformat()


def window():
    return datetime.now(MSK).hour >= 23 or datetime.now(MSK).hour < 5


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def source(out):
    token = os.environ.get('RAPID_API_AUTHKEY')
    if not token:
        raise RuntimeError('RAPID_API_AUTHKEY missing')
    try:
        response = requests.get('https://b2b-rapid1.ru/api/export.php',
                                params={'authkey': token, 'type': 'json'}, timeout=(15, 60))
    except requests.RequestException:
        raise RuntimeError('Rapid catalogue request failed') from None
    if response.status_code != 200:
        raise RuntimeError(f'Rapid HTTP {response.status_code}')
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError('Rapid response is not JSON') from None
    if data.get('error', {}).get('code') != 0 or not isinstance(data.get('price'), list):
        raise RuntimeError('Rapid catalogue rejected')
    save(out / 'catalogue.json', data)
    return data


def build_plan(cards, items):
    """Exact supplier article only. Zero stock is included; duplicates/empty names skipped."""
    normalize = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    index = defaultdict(list)
    for row in items:
        if str(row.get('CodeID') or '').strip():
            index[normalize(row['CodeID'])].append(row)
    plan, counts = [], Counter()
    for card in cards:
        if card.get('archived') or ms_api.meta_id(card, 'supplier') not in IDS:
            counts['archived_or_foreign'] += 1
            continue
        found = index.get(normalize(card.get('article')), [])
        if len(found) != 1:
            counts['article_missing_or_ambiguous'] += 1
            continue
        row = found[0]
        name = row.get('Name')
        if not isinstance(name, str) or not name.strip():
            counts['empty_source_name'] += 1
            continue
        if (card.get('description') or '') == name:
            counts['already_equal'] += 1
            continue
        plan.append({'id': card['id'], 'article': card.get('article') or '',
                     'supplier_id': ms_api.meta_id(card, 'supplier'),
                     'old_description': card.get('description') or '', 'description': name,
                     'api_free_stock': row.get('UnitsFree'), 'external_code': card.get('externalCode') or ''})
    return sorted(plan, key=lambda r: r['id']), dict(counts)


def preview(out):
    data = source(out)
    cards = {}
    for sid in sorted(IDS):
        for card in ms_api.iter_rows('/entity/product',
                                    {'filter': f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'}, page=500):
            cards[card['id']] = card
    save(out / 'ms_before.json', list(cards.values()))
    plan, skipped = build_plan(cards.values(), data['price'])
    save(out / 'plan.json', plan)
    article_key = lambda value: re.sub(r'\s+', '', str(value or '')).upper()
    article_counts = Counter(article_key(r.get('CodeID')) for r in data['price'] if r.get('CodeID'))
    unmatched = [{'id': c['id'], 'external_code': c.get('externalCode') or '',
                  'article': c.get('article') or '', 'api_matches': article_counts[article_key(c.get('article'))]}
                 for c in cards.values() if article_counts[article_key(c.get('article'))] != 1]
    save(out / 'unmatched.json', unmatched)
    summary = {'at': stamp(), 'supplier_cards': len(cards), 'catalogue_items': len(data['price']),
               'changes': len(plan), 'skipped': skipped,
               'changes_with_zero_free_stock': sum(str(r['api_free_stock']).strip() in ('0', '0.0', 'no') for r in plan),
               'supplier_stock_filter': False, 'archive_excluded': True, 'write_fields': ['description'],
               'mapping': 'supplier group + unique article after case/whitespace normalization',
               'max_changes': CAP, 'writes': 0}
    save(out / 'summary.json', summary)
    with (out / 'plan.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=['id', 'external_code', 'article', 'supplier_id',
                                              'api_free_stock', 'old_description', 'description'], delimiter=';')
        writer.writeheader()
        writer.writerows(plan)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return plan, summary


def log(out, event):
    with (out / 'journal.jsonl').open('a') as fh:
        fh.write(json.dumps({'at': stamp(), **event}, ensure_ascii=False) + '\n')
        fh.flush()
        os.fsync(fh.fileno())


def checked_write_payload(live, row):
    if live.get('archived') or ms_api.meta_id(live, 'supplier') != row['supplier_id'] or live.get('article') != row['article']:
        raise RuntimeError('Card identity changed; stop')
    if live.get('externalCode', '') != row['external_code']:
        raise RuntimeError('External code changed; stop')
    if (live.get('description') or '') == row['description']:
        return None
    if (live.get('description') or '') != row['old_description']:
        raise RuntimeError('Description changed since snapshot; stop')
    return {'id': row['id'], 'description': row['description']}


def apply(out, plan, summary, allow_daytime=False, resume=False, before_write=None, fresh_canary=False):
    if len(plan) > CAP:
        raise RuntimeError('Plan exceeds one-night cap; stop before writes')
    if (out / 'journal.jsonl').exists() and not resume:
        raise RuntimeError('Write journal exists; no automatic duplicate run')
    # Own timer must not compete with the existing mass-write job.
    import subprocess
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'prc-enter-buyprice.service']).returncode == 0:
        raise RuntimeError('Buyprice writer still running; stop before writes')
    total = summary.get('writes', 0) if resume else 0
    verified = summary.get('verified', 0) if resume else 0
    canary_target = total + 200 if fresh_canary else 200
    canary_done = False if fresh_canary else total >= 200
    planned_count = summary['changes']
    backup_prefix = 'backup_resume_' + datetime.now(MSK).strftime('%H%M%S') if resume else 'backup_batch'
    for start in range(0, len(plan), BATCH):
        if before_write:
            before_write()
        if not allow_daytime and not window():
            raise RuntimeError('Outside 23:00-05:00 MSK; stop')
        rows = plan[start:start + BATCH]
        payload, originals = [], []
        for row in rows:
            time.sleep(0.2)
            live = ms_api.get(f"/entity/product/{row['id']}")
            patch = checked_write_payload(live, row)
            if patch:
                originals.append(live)
                payload.append(patch)
            else:
                log(out, {'phase': 'verified', 'id': row['id'], 'already_desired_on_readback': True})
                verified += 1
        if not payload:
            continue
        save(out / f'{backup_prefix}_{start:05d}.json', originals)
        if not allow_daytime and not window():
            raise RuntimeError('Outside night window before write')
        log(out, {'phase': 'sending', 'ids': [r['id'] for r in payload]})
        if before_write and before_write():
            # A competing writer may have changed a card during the pause.
            for row in rows:
                live = ms_api.get(f"/entity/product/{row['id']}")
                patch = checked_write_payload(live, row)
                if patch and patch not in payload:
                    raise RuntimeError('Payload changed during writer pause; stop')
                if not patch and any(p['id'] == row['id'] for p in payload):
                    raise RuntimeError('Card changed during writer pause; inspect before resume')
        before = time.monotonic()
        # No automatic retry of writes: a timeout is uncertain and requires readback.
        try:
            response = requests.post(ms_api.BASE + '/entity/product', json=payload,
                                     headers={'Authorization': f'Bearer {ms_api.TOKEN}'}, timeout=(15, 120))
        except requests.RequestException:
            raise RuntimeError('Uncertain MS write; inspect journal/read back before resuming') from None
        if response.status_code != 200:
            raise RuntimeError(f'MS write HTTP {response.status_code}; inspect journal')
        answer = response.json()
        if not isinstance(answer, list) or {r.get('id') for r in answer} != {r['id'] for r in payload}:
            raise RuntimeError('Unexpected MS write response; stop')
        elapsed = time.monotonic() - before
        total += len(payload)
        for patch in payload:
            time.sleep(0.2)
            live = ms_api.get(f"/entity/product/{patch['id']}")
            if live.get('description') != patch['description']:
                raise RuntimeError('Readback mismatch; stop')
            log(out, {'phase': 'verified', 'id': patch['id']})
            verified += 1
        summary.update({'at': stamp(), 'writes': total, 'verified': verified, 'status': 'running'})
        save(out / 'summary.json', summary)
        print(f'{stamp()} verified {verified}/{planned_count}', flush=True)
        if start + BATCH < len(plan):
            time.sleep(120 if elapsed > 10 else PAUSE)
        if total >= canary_target and not canary_done:
            canary_done = True
            time.sleep(600)
            # Canary: authenticated document read and bounded response latency.
            before = time.monotonic()
            ms_api.get('/entity/customerorder', {'limit': 1})
            if time.monotonic() - before > 10:
                raise RuntimeError('Canary document read slow; stop')
    summary.update({'at': stamp(), 'writes': total, 'verified': verified, 'status': 'complete'})
    summary.pop('error_type', None)
    if verified != planned_count:
        raise RuntimeError('Final verified count differs from frozen plan')
    save(out / 'summary.json', summary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--allow-daytime', action='store_true',
                        help='Explicit user-authorized immediate run; default night guard remains')
    parser.add_argument('--resume', action='store_true', help='Read back and complete unverified IDs from the frozen plan')
    args = parser.parse_args()
    out = BASE_OUT / ('night' if args.apply else 'dry_run')
    out.mkdir(parents=True, exist_ok=True)
    with (BASE_OUT / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.apply and not args.allow_daytime and not window():
            raise RuntimeError('Outside 23:00-05:00 MSK')
        if args.apply and (out / 'journal.jsonl').exists() and not args.resume:
            raise RuntimeError('Prior write journal exists; inspect before another run')
        if args.resume:
            if not args.apply:
                raise RuntimeError('Resume requires --apply')
            original_plan = json.loads((out / 'plan.json').read_text())
            summary = json.loads((out / 'summary.json').read_text())
            events = [json.loads(line) for line in (out / 'journal.jsonl').read_text().splitlines()]
            done = {r['id'] for r in events if r['phase'] == 'verified'}
            if not done <= {r['id'] for r in original_plan} or len(done) != summary.get('verified'):
                raise RuntimeError('Journal and summary disagree')
            plan = [r for r in original_plan if r['id'] not in done]
            summary['resume_count'] = summary.get('resume_count', 0) + 1
            log(out, {'phase': 'resume', 'remaining_ids': [r['id'] for r in plan]})
            print(f'Resuming {len(plan)} unverified cards; {len(done)} verified cards excluded', flush=True)
        else:
            plan, summary = preview(out)
        if args.apply:
            summary['explicit_daytime_authorization'] = args.allow_daytime
            save(out / 'summary.json', summary)
            apply(out, plan, summary, allow_daytime=args.allow_daytime, resume=args.resume)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        failure_out = BASE_OUT / ('night' if '--apply' in sys.argv else 'dry_run')
        old = json.loads((failure_out / 'summary.json').read_text()) if (failure_out / 'summary.json').exists() else {}
        old.update({'at': stamp(), 'status': 'failed', 'error_type': type(exc).__name__})
        save(failure_out / 'summary.json', old)
        raise

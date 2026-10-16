#!/usr/bin/env python3
# поток: card
"""Owner-authorized remaining TN VED updates, fresh pages and verified checkpoints."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from collectors.wb import _content_token, CARDS_URL
from tools.card_wb_tnved_dryrun import pilot_request, code_of
from tools.card_wb_tnved_rule_recheck import char, fetch
from tools.card_wb_tnved_apply_203 import preservation

OUT = ROOT / 'docs/reports/wb_tnved_remaining_2026-10-05'
FIELDS = (15000001, 15004139)
UPDATE = 'https://content-api.wildberries.ru/content/v2/cards/update'

def now():
    return datetime.now(timezone.utc).isoformat()

def save(name, data):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as h:
        json.dump(data, h, ensure_ascii=False)
        h.flush(); os.fsync(h.fileno())
    os.replace(tmp, path)

def journal(data):
    with (OUT / 'journal.jsonl').open('a') as h:
        h.write(json.dumps({'at': now(), **data}, ensure_ascii=False) + '\n')
        h.flush(); os.fsync(h.fileno())

def scope():
    source = ROOT / 'docs/reports/wb_tnved_dry_run_2026-10-05/plan.csv'
    rows = list(csv.DictReader(source.open(encoding='utf-8-sig'), delimiter=';'))
    completed = json.loads((ROOT / 'docs/reports/wb_tnved_apply_203_2026-10-05/results.json').read_text())
    done = {(r['account'], int(r['nmID'])) for r in completed.values() if r.get('status') == 'VERIFIED'}
    result = {}
    for r in rows:
        ident = (r['account'], int(r['Артикул ВБ']))
        if ident in done or r['dry_run_status'] == 'NO_SOURCE' or r['Внешний код карточки'] == '4434UU1ZP70G':
            continue
        result[ident] = {'account': ident[0], 'nmID': ident[1], 'vendorCode': r['Внешний код карточки'],
                         'ink_code': code_of(r['ТН ВЭД в ТК'])}
    if len(rows) != 16592 or len(result) != 16365:
        raise ValueError(f'Unexpected bounded scope: {len(rows)}, {len(result)}')
    return result

def page(session, account, cursor):
    body = {'settings': {'sort': {'ascending': True}, 'cursor': cursor, 'filter': {'withPhoto': -1}}}
    for attempt in range(6):
        response = session.post(CARDS_URL, headers={'Authorization': _content_token(account)}, json=body, timeout=45)
        time.sleep(.7)
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(min(30, max(3, int(response.headers.get('Retry-After', '3')))))
            continue
        response.raise_for_status()
        data = response.json()
        if data.get('error') or not isinstance(data.get('cards'), list):
            raise ValueError('Invalid catalog read')
        return data
    raise RuntimeError('Read retries exhausted')

def verified(before, after, code):
    return all(char(after, field) == code for field in FIELDS) and preservation(before) == preservation(after)

def verify_batch(session, pending, state):
    targets = {c['nmID']: c for c in pending['before']}
    found = {}
    deadline = time.monotonic() + 180
    while targets.keys() - found.keys():
        cursor = dict(pending['verify_cursor'])
        # WB silently ignores the entire cursor when nmID is zero.
        if not cursor.get('nmID'):
            cursor['nmID'] = next(iter(targets))
        cursors = set()
        while True:
            data = page(session, pending['account'], cursor)
            for card in data['cards']:
                nm = card['nmID']
                if nm in targets:
                    code = pending['codes'][str(nm)]
                    if all(char(card, field) == code for field in FIELDS):
                        if not verified(targets[nm], card, code):
                            save('preservation_failure.json', {'before': targets[nm], 'after': card})
                            raise ValueError(f'Content preservation failed: {nm}')
                        found[nm] = card
            if len(data['cards']) < cursor['limit'] or not (targets.keys() - found.keys()):
                break
            cur = data['cursor']
            marker = (cur['updatedAt'], cur['nmID'])
            if marker in cursors:
                raise ValueError('Verification cursor stalled')
            cursors.add(marker)
            cursor = {'limit': 100, 'updatedAt': marker[0], 'nmID': marker[1]}
        if not (targets.keys() - found.keys()):
            break
        if time.monotonic() > deadline:
            # Clock-skew/cursor fallback uses exact identities, never resends.
            for nm in targets.keys() - found.keys():
                card = fetch(session, pending['account'], nm)
                if not card or not verified(targets[nm], card, pending['codes'][str(nm)]):
                    raise RuntimeError(f'Update not verified: {nm}; no automatic resend')
                found[nm] = card
            break
        time.sleep(5)
    save(f"after/{pending['batch']}.json", list(found.values()))
    for nm in targets:
        state['results'][f"{pending['account']}:{nm}"] = {'status': 'VERIFIED', 'code': pending['codes'][str(nm)], 'at': now()}
    state.pop('pending', None)
    save('state.json', state)
    journal({'phase': 'verified', 'batch': pending['batch'], 'count': len(targets)})

def status(state, scope_rows):
    counts = Counter(r['status'] for r in state['results'].values())
    save('summary.json', {'at': now(), 'phase': state['phase'], 'scope': len(scope_rows),
                         'counts': dict(counts), 'remaining_unobserved': len(scope_rows) - len(state['results']),
                         'batches': state['batch'], 'error': state.get('error')})
    print(now(), state['phase'], dict(counts), 'batches', state['batch'], flush=True)

def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    lock = (OUT / 'worker.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rows = scope()
    save('authorization.json', {'at': now(), 'instruction': 'отправляй остальным карточкам код ТН ВЭД',
         'scope': len(rows), 'scope_sha256': hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),
         'batch_limit': 50, 'cartridge_code': '8443999000', 'ink_code_source': 'saved TC',
         'exclude': ['retired 23', 'verified 203', '4434UU1ZP70G'], 'mode': 'apply' if args.apply else 'preview'})
    if not args.apply:
        print('Preview: bounded scope', len(rows), 'no API writes'); return
    state_file = OUT / 'state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {'results': {}, 'cursors': {}, 'finished_accounts': [], 'batch': 0}
    state['phase'] = 'RUNNING'; state.pop('error', None)
    save('state.json', state)
    try:
        with requests.Session() as session:
            dictionaries = {}
            for subject in (1820, 1907):
                response = session.get(f'https://content-api.wildberries.ru/content/v2/directory/tnved?subjectID={subject}',
                    headers={'Authorization': _content_token('wb_acc1')}, timeout=45)
                response.raise_for_status()
                dictionary = response.json(); save(f'dictionary_{subject}.json', dictionary)
                dictionaries[subject] = {str(r['tnved']) for r in dictionary['data']}
                time.sleep(.7)
            if '8443999000' not in dictionaries[1820]:
                raise ValueError('Cartridge dictionary changed')
            if state.get('pending'):
                verify_batch(session, state['pending'], state)
            for account in ('wb_acc1', 'wb_acc2'):
                if account in state['finished_accounts']:
                    continue
                cursor = state['cursors'].get(account, {'limit': 50})
                seen_cursors = set()
                while True:
                    data = page(session, account, cursor)
                    cards = []; codes = {}; payload = []
                    for card in data['cards']:
                        ident = (account, int(card['nmID'])); k = f'{account}:{card["nmID"]}'
                        if ident not in rows or k in state['results']:
                            continue
                        row = rows[ident]
                        if card['vendorCode'] != row['vendorCode']:
                            raise ValueError('Exact vendor identity changed')
                        reason = None
                        if any(char(card, field) for field in FIELDS): reason = 'ALREADY_FILLED'
                        elif card['subjectID'] not in dictionaries: reason = 'REVIEW_SUBJECT'
                        elif card.get('documents', {}).get('items'): reason = 'REVIEW_ATTACHED_DOCUMENTS'
                        code = '8443999000' if card['subjectID'] == 1820 else row['ink_code']
                        if not reason and code not in dictionaries[card['subjectID']]: reason = 'REVIEW_CODE'
                        if reason:
                            state['results'][k] = {'status': reason}; continue
                        request = pilot_request(card, code)[0]
                        if 'documents' in card:
                            request['documents'] = {'items': [], 'excludeDocuments': card['documents'].get('excludeDocuments', False)}
                        cards.append(card); codes[str(card['nmID'])] = code; payload.append(request)
                    if cards:
                        state['batch'] += 1
                        bid = f'{account}_{state["batch"]:05d}'
                        # Query WB changes since before this write, with overlap for clock skew.
                        start = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat().replace('+00:00', 'Z')
                        pending = {'batch': bid, 'account': account, 'before': cards, 'codes': codes,
                                   'verify_cursor': {'limit': 100, 'updatedAt': start, 'nmID': cards[0]['nmID']}}
                        save(f'before/{bid}.json', cards); save(f'requests/{bid}.json', payload)
                        state['pending'] = pending; save('state.json', state)
                        journal({'phase': 'intent', 'batch': bid, 'account': account, 'count': len(cards)})
                        response = session.post(UPDATE, headers={'Authorization': _content_token(account)}, json=payload, timeout=45)
                        answer = response.json() if response.content else {}
                        save(f'responses/{bid}.json', {'http': response.status_code, 'body': answer})
                        journal({'phase': 'response', 'batch': bid, 'http': response.status_code, 'body': answer})
                        response.raise_for_status()
                        if answer.get('error'): raise RuntimeError('WB rejected batch')
                        time.sleep(6.1)
                        verify_batch(session, pending, state)
                    if len(data['cards']) < 50:
                        state['finished_accounts'].append(account); save('state.json', state); status(state, rows); break
                    cur = data['cursor']; marker = (cur['updatedAt'], cur['nmID'])
                    if marker in seen_cursors: raise ValueError('Catalog cursor stalled')
                    seen_cursors.add(marker)
                    cursor = {'limit': 50, 'updatedAt': marker[0], 'nmID': marker[1]}
                    state['cursors'][account] = cursor; save('state.json', state)
                    status(state, rows)
            for account, nm in rows:
                state['results'].setdefault(f'{account}:{nm}', {'status': 'NOT_OBSERVED_ACTIVE'})
            state['phase'] = 'COMPLETED'
            save('state.json', state); status(state, rows)
    except Exception as error:
        state['phase'] = 'STOPPED'; state['error'] = f'{type(error).__name__}: {error}'
        save('state.json', state); status(state, rows)
        raise

if __name__ == '__main__':
    run()

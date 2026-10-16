#!/usr/bin/env python3
# поток: card
"""Frozen 364-card own-supplier dimensions plan; no daytime MS writes."""
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from prices import blacklist
from tools.card_dimensions_source_inventory import sparse_xlsx, triple
from tools.card_dimensions_own_source_plan import SOURCE_HOLDS, key, num
from tools.card_rapid_dimensions_night import patch as merge_empty_dimensions

OUT = ROOT / 'docs/reports/card_dimensions_own_source_night_2026-10-15'
PROPOSAL = ROOT / 'docs/reports/card_dimensions_own_source_plan_2026-10-15'
START = datetime(2026, 10, 15, 20, 20, tzinfo=timezone.utc)
END = datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)
HOOK_ID = 'ed64d51f-6dfc-11f1-0a80-0e6f00770df1'
HOOK_SHA = '1948a8bd84aedfd9359c314807165f55f118a8518cb4bb5d56c95e702a762103'
HEADER = 'X-Lognex-WebHook-DisableByPrefix'
MODE = 'request_scoped_exact_TC_prefix_no_subscription_mutations'
AXES = ['Длина, см.', 'Ширина, см.', 'Высота, см.']
WRITERS = ['prc-enter-buyprice.service', 'card-solutionsprint-resolved-20261013.service',
           'card-supplier-ean13-20261013.service', 'card-rapid-dimensions-20261014.service']


def save(name, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def log(**row):
    with (OUT / 'journal.jsonl').open('a') as f:
        f.write(json.dumps({'at_utc': datetime.now(timezone.utc).isoformat(), **row}, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def checked_prefix():
    """Never log/return the authenticated subscription URL to artifacts."""
    data = ms_api.get('/entity/webhook', {'limit': 1000})
    if data['meta']['size'] != len(data['rows']):
        raise RuntimeError('Incomplete webhook inventory')
    active = [h for h in data['rows'] if h.get('enabled') and
              h.get('entityType') == 'product' and h.get('action') == 'UPDATE']
    if len(active) != 1:
        raise RuntimeError('Unexpected product UPDATE consumers')
    hook = active[0]
    url = hook.get('url', '')
    if hook.get('id') != HOOK_ID or hashlib.sha256(url.encode()).hexdigest() != HOOK_SHA:
        raise RuntimeError('Frozen TC subscription changed')
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.netloc or '\n' in url or '\r' in url:
        raise RuntimeError('Invalid webhook prefix')
    url.encode('latin-1')
    return url


def source_gate(plan, hashes):
    """Reparse original Excel cells, not a derived size table or other suppliers."""
    for path, expected in hashes.items():
        if sha(ROOT / path) != expected:
            raise RuntimeError('Original supplier source changed')
    suppliers = {r['source_file']: r['supplier'] for r in plan}
    index = defaultdict(list)
    for path, supplier in suppliers.items():
        if path not in hashes:
            raise RuntimeError('Unfrozen source')
        for sheet, rn, cells in sparse_xlsx(ROOT / path):
            if rn == 1:
                continue
            if supplier == 'Кактус':
                article, vals, factor = cells.get('D'), [cells.get(c) for c in ['K', 'M', 'L']], Decimal('.1')
            elif supplier == 'Изипринт' and sheet == 'Данные по картриджам':
                article, vals, factor = cells.get('A'), triple(cells.get('R')), Decimal('.1')
            elif supplier == 'Сакура':
                mapping = {'Лазерная Sakura': ('E', ['Z', 'AA', 'AB'], '100'),
                           'Струйная Sakura': ('A', ['Z', 'AA', 'AB'], '.1'),
                           'Матричная Sakura': ('A', ['T', 'U', 'V'], '.1'),
                           'Наклейки SOK': ('A', ['AD', 'AE', 'AF'], '1')}
                if sheet not in mapping:
                    continue
                ac, cols, mult = mapping[sheet]
                article, vals, factor = cells.get(ac), [cells.get(c) for c in cols], Decimal(mult)
            else:
                continue
            if article and vals and all(num(v) for v in vals):
                ds = tuple(Decimal(format(num(v), '.15g')) * factor for v in vals)
                index[(supplier, key(article))].append((ds, path, sheet, str(rn)))
    for row in plan:
        k = (row['supplier'], key(row['article']))
        found = index.get(k, [])
        wanted = tuple(Decimal(a['value']) for a in row['dimensions'])
        if k in SOURCE_HOLDS or {r[0] for r in found} != {wanted}:
            raise RuntimeError('Own source no longer uniquely matches frozen dimensions')
        if not any((r[1], r[2], r[3]) == (row['source_file'], row['source_sheet'], str(row['source_row'])) for r in found):
            raise RuntimeError('Original source pointer mismatch')


def patch(live, row, black):
    if blacklist.norm(row['article']) in black:
        raise RuntimeError('BLACKLIST')
    if blacklist.in_damaged(live.get('name')):
        raise RuntimeError('DAMAGED_PACKAGE')
    if live.get('name') != row['name']:
        raise RuntimeError('NAME_CHANGED_REVIEW_REQUIRED')
    # Identity/archival/stop/duplicate gates are checked even on already filled cards.
    body = merge_empty_dimensions(live, row)
    if body is None:
        return None
    targets = {a['id'] for a in row['dimensions']}
    if any(a.get('id') in targets and str(a.get('value') or '').strip()
           for a in live.get('attributes', [])):
        raise RuntimeError('DIMENSIONS_NO_LONGER_ALL_EMPTY')
    return body


def guard():
    waited = False
    while True:
        if not START <= datetime.now(timezone.utc) < END:
            raise RuntimeError('Outside fixed night window')
        if not any(subprocess.run(['systemctl', 'is-active', '--quiet', u]).returncode == 0 for u in WRITERS):
            return waited
        waited = True
        time.sleep(30)


def probe():
    guard()
    started = time.monotonic()
    ms_api.get('/entity/customerorder', {'limit': 1})
    if time.monotonic() - started > 10:
        raise RuntimeError('Operational read is slow')


def prepare():
    if (OUT / 'authorization.json').exists() or (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Do not overwrite existing authorization or execution')
    integrity = load(PROPOSAL / 'proposal_integrity.json')
    if sha(PROPOSAL / 'candidate_fill.json') != integrity['candidate_fill_sha256']:
        raise RuntimeError('Proposal changed')
    rows = load(PROPOSAL / 'candidate_fill.json')
    if len(rows) != 364 or len({r['ms_id'] for r in rows}) != 364:
        raise RuntimeError('Unexpected scope')
    metadata = ms_api.get('/entity/product/metadata/attributes')['rows']
    attrs = []
    for name in AXES:
        found = [a for a in metadata if a['name'] == name and a['type'] == 'string']
        if len(found) != 1:
            raise RuntimeError('Dimension attribute metadata mismatch')
        attrs.append(found[0])
    plan = []
    for r in rows:
        if r['reasons'] or r['status'] != 'CANDIDATE_FILL' or r['current_cm']:
            raise RuntimeError('Unexpected proposal row')
        plan.append({**r, 'id': r['ms_id'], 'dimensions': [
            {'id': a['id'], 'meta': a['meta'], 'name': a['name'], 'type': 'string', 'value': v}
            for a, v in zip(attrs, r['dimensions_cm'])]})
    plan.sort(key=lambda r: r['id'])
    source_gate(plan, integrity['source_sha256'])
    checked_prefix()
    black = blacklist.load_set()
    def read(row):
        live = ms_api.get('/entity/product/' + row['id'])
        save('preflight/' + row['id'] + '.json', live)
        try:
            result = patch(live, row, black)
            return {'id': row['id'], 'status': 'ready' if result else 'already_desired'}
        except RuntimeError as exc:
            return {'id': row['id'], 'status': 'skip', 'reason': str(exc)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        checks = list(pool.map(read, plan))
    save('preflight.json', checks)
    save('plan.json', plan)
    digest = sha(OUT / 'plan.json')
    save('authorization.json', {'authorized': True, 'instruction': 'да — согласование плана 364 карточек',
         'authorized_at_utc': datetime.now(timezone.utc).isoformat(), 'plan_sha256': digest,
         'source_sha256': integrity['source_sha256'], 'notification_mode': MODE,
         'user_notified_in_conversation': True, 'stock_filter': False, 'cross_supplier_comparison': False,
         'external_TC_polling_recovery_verified': False,
         'queue_protection': 'dimension requests suppress own TC notification; all subscriptions remain enabled'})
    save('summary.json', {'status': 'authorized_ready_not_scheduled', 'cards': 364, 'fields': 1092,
         'plan_sha256': digest, 'preflight': dict(Counter(r['status'] for r in checks)),
         'start_msk': '2026-10-15 23:20', 'cutoff_msk': '2026-10-16 05:00',
         'batch_size': 50, 'pause_seconds': 60, 'canary_cards': 200, 'canary_pause_seconds': 600,
         'writes': 0, 'verified': 0, 'skipped': 0, 'already_desired': 0, 'notification_mode': MODE})
    print(json.dumps({'cards': len(plan), 'preflight': Counter(r['status'] for r in checks), 'MS_writes': 0}))


def send(payload, prefix):
    if not START <= datetime.now(timezone.utc) < END:
        raise RuntimeError('Outside night window before POST')
    log(phase='sending', ids=[r['id'] for r in payload])
    try:
        response = requests.post(ms_api.BASE + '/entity/product', json=payload,
            headers={'Authorization': 'Bearer ' + ms_api.TOKEN, HEADER: prefix}, timeout=(15, 120))
    except requests.RequestException:
        raise RuntimeError('Uncertain MS write; audit required; no retry') from None
    if response.status_code != 200:
        raise RuntimeError('MS write rejected; audit required; no retry')
    data = response.json()
    if not isinstance(data, list) or len(data) != len(payload) or {r.get('id') for r in data} != {r['id'] for r in payload}:
        raise RuntimeError('MS response ID mismatch; audit required')


def verify(live, body, before):
    if {a['id']: a.get('value') for a in live.get('attributes', [])} != {a['id']: a.get('value') for a in body['attributes']}:
        raise RuntimeError('Readback attributes mismatch')
    for field in ['name', 'article', 'externalCode', 'code', 'description', 'supplier', 'barcodes', 'weight', 'volume', 'archived']:
        if live.get(field) != before.get(field):
            raise RuntimeError('Other field changed during readback: ' + field)


def apply():
    auth, summary = load(OUT / 'authorization.json'), load(OUT / 'summary.json')
    if not auth.get('authorized') or auth.get('notification_mode') != MODE:
        raise RuntimeError('Not authorized')
    if (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Existing journal: manual audit before any resumption')
    if sha(OUT / 'plan.json') != auth['plan_sha256'] or auth['plan_sha256'] != summary['plan_sha256']:
        raise RuntimeError('Frozen plan changed')
    guard()
    plan = load(OUT / 'plan.json')
    source_gate(plan, auth['source_sha256'])
    checked_prefix()
    probe()
    summary['status'] = 'running'
    save('summary.json', summary)
    canary_done = False
    for start in range(0, len(plan), 50):
        guard()
        while True:
            black = blacklist.load_set()
            before, payload, skipped, already = {}, [], [], []
            for row in plan[start:start + 50]:
                live = ms_api.get('/entity/product/' + row['id'])
                try:
                    body = patch(live, row, black)
                except RuntimeError as exc:
                    skipped.append({'id': row['id'], 'reason': str(exc)})
                    continue
                if body is None:
                    already.append(row['id'])
                else:
                    before[row['id']] = live
                    payload.append(body)
                time.sleep(.2)
            if not guard():
                break
        save(f'backup_batch_{start:04d}.json', list(before.values()))
        save(f'payload_batch_{start:04d}.json', payload)
        for r in skipped:
            log(phase='skipped', **r)
        for pid in already:
            log(phase='already_desired', id=pid)
        summary['skipped'] += len(skipped)
        summary['already_desired'] += len(already)
        elapsed = 0
        if payload:
            prefix = checked_prefix()
            started = time.monotonic()
            send(payload, prefix)
            elapsed = time.monotonic() - started
            summary['writes'] += len(payload)
            save('summary.json', summary)
            for body in payload:
                live = ms_api.get('/entity/product/' + body['id'])
                save('after/' + body['id'] + '.json', live)
                verify(live, body, before[body['id']])
                log(phase='verified', id=body['id'])
                summary['verified'] += 1
                time.sleep(.2)
            checked_prefix()
        save('summary.json', summary)
        if not canary_done and start + 50 >= 200:
            log(phase='canary_pause_start', verified=summary['verified'])
            for _ in range(20):
                time.sleep(30)
                guard()
            probe()
            canary_done = True
            log(phase='canary_health_passed')
        elif start + 50 < len(plan):
            time.sleep(120 if elapsed > 10 else 60)
    if summary['verified'] + summary['skipped'] + summary['already_desired'] != len(plan):
        raise RuntimeError('Final count mismatch')
    summary.update(status='complete', completed_at_utc=datetime.now(timezone.utc).isoformat())
    save('summary.json', summary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            prepare() if args.prepare else apply()
        except Exception as exc:
            if args.apply and (OUT / 'summary.json').exists():
                summary = load(OUT / 'summary.json')
                if summary.get('status') != 'complete':
                    summary.update(status='stopped_requires_audit', error_type=type(exc).__name__)
                    save('summary.json', summary)
            print('Stopped safely: ' + type(exc).__name__, flush=True)
            raise SystemExit(1)

# поток: prc
"""One authorized WB-name pilot: 200 cards at 21:00 MSK, no continuation."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core import db, ms_api
from prices import blacklist
from prices.wb_label import RULES_VERSION, fits_two_lines
from tools.prc.wb_fill import compose_details
from tools.prc.wb_name_audit import identity_issues, missing_fields
from tools.prc.wb_name_preflight import digest as source_digest

OUT = ROOT / 'docs/reports/prc_wb_name_canary_night_2026-10-15'
PREFLIGHT = ROOT / 'docs/reports/prc_wb_name_canary_preflight_2026-10-15'
DIMENSION_LOCK = ROOT / 'docs/reports/card_dimensions_weight_night_2026-10-15/lock'
START = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
WRITE_END = datetime(2026, 10, 15, 18, 20, tzinfo=timezone.utc)
END = datetime(2026, 10, 15, 18, 45, tzinfo=timezone.utc)
MIN_FREE = 16 * 1024 * 1024
CODE_FILES = ('tools/prc/wb_name_canary.py', 'tools/prc/wb_name_preflight.py',
              'tools/prc/wb_name_audit.py', 'tools/prc/wb_fill.py',
              'prices/wb_label.py', 'prices/data/wb_label_font_metrics.json',
              'prices/features.py', 'prices/catalog.py', 'prices/blacklist.py')
WRITER = re.compile(r'(?:card-.*(?:dimensions|weight).*|prc-enter-buyprice)')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(name):
    return json.loads((OUT / name).read_text())


def save(name, value):
    path = OUT / name
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    if name.endswith('.gz'):
        data = gzip.compress(data)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def journal(phase, **data):
    record = dict(at_utc=datetime.now(timezone.utc).isoformat(), phase=phase, **data)
    with (OUT / 'journal.jsonl').open('a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())


def unit_inventory(kind):
    command = ['systemctl', 'list-timers', '--all', '--output=json', '--no-pager'] if kind == 'timers' else [
        'systemctl', 'list-units', '--all', '--type=service', '--output=json', '--no-pager']
    return json.loads(subprocess.check_output(command, text=True))


def active_writers(units):
    return [u['unit'] for u in units if WRITER.fullmatch(u.get('unit', '').removesuffix('.service'))
            and u.get('active') not in ('inactive', 'failed')]


def check_timer_conflicts(timers):
    matches = []
    for row in timers:
        unit = row.get('unit', '').removesuffix('.timer')
        if not WRITER.fullmatch(unit) or not row.get('next'):
            continue
        start = datetime.fromtimestamp(row['next'] / 1_000_000, timezone.utc)
        matches.append({'unit': row['unit'], 'next_utc': start.isoformat()})
        if START <= start < END:
            raise RuntimeError('Overlapping mass-writer timer: ' + row['unit'])
    return matches


def guard_window(now, writing):
    if not START <= now < (WRITE_END if writing else END):
        raise RuntimeError('Outside authorized pilot window')


def guard(writing=True):
    guard_window(datetime.now(timezone.utc), writing)
    if shutil.disk_usage(OUT).free < MIN_FREE:
        raise RuntimeError('Insufficient free space for backup/journal')
    writers = active_writers(unit_inventory('services'))
    if writers:
        raise RuntimeError('Another mass writer is active: ' + ', '.join(writers))
    check_timer_conflicts(unit_inventory('timers'))


def probe():
    started = time.monotonic()
    ms_api.get('/entity/customerorder', {'limit': 1})
    elapsed = time.monotonic() - started
    if elapsed > 10:
        raise RuntimeError('Operational read slower than 10 seconds')
    return round(elapsed, 3)


def target_value(product, aid):
    found = [a for a in product.get('attributes', []) if a.get('name') == 'Название WB'
             or a.get('id') == aid or a.get('meta', {}).get('href', '').endswith('/' + aid)]
    if len(found) > 1:
        raise RuntimeError('Duplicate WB-name attribute')
    return str(found[0].get('value') or '') if found else ''


def patch(product, row, raw, aid, black):
    for field, expected in [('id', row['ms_id']), ('externalCode', row['external_code']),
                            ('code', row['code']), ('name', row['supplier_name']),
                            ('article', row['article']), ('supplier', row['supplier'])]:
        if product.get(field) != expected:
            raise RuntimeError('Product identity changed: ' + field)
    if product.get('archived') or blacklist.norm(row['article']) in black:
        raise RuntimeError('Archived/blacklisted product')
    if raw is None or source_digest(raw) != row['tc_semantic_sha256']:
        raise RuntimeError('TC semantic source changed')
    result = compose_details(raw, product.get('name') or '')
    if result.name != row['new'] or not result.name or not fits_two_lines(result.name):
        raise RuntimeError('Common formula changed or no longer fits')
    attrs = product.get('attributes', [])
    values = {a['name']: a.get('value') for a in attrs}
    issues = identity_issues(product, raw) + missing_fields(product, result.name)
    if values.get('Стоп лист') or re.search(r'брак|стоп.?лист|оригинал|\bOEM\b',
                                           product.get('pathName', '') + ' ' + product.get('name', ''), re.I):
        issues.append('Stop/damaged/original gate')
    if issues:
        raise RuntimeError('; '.join(issues))
    current = target_value(product, aid)
    if current == row['new']:
        return None
    if current != row['old']:
        raise RuntimeError('WB name changed after preflight')
    updated = [a for a in attrs if a.get('name') != 'Название WB'
               and a.get('id') != aid and not a.get('meta', {}).get('href', '').endswith('/' + aid)]
    updated.append({'meta': {'href': ms_api.BASE + '/entity/product/metadata/attributes/' + aid,
                             'type': 'attributemetadata', 'mediaType': 'application/json'},
                    'type': 'text', 'value': row['new']})
    return {'id': row['ms_id'], 'attributes': updated}


def other_attributes(product, aid):
    return {a.get('id') or a.get('name') or a.get('meta', {}).get('href'):
            {'type': a.get('type'), 'value': a.get('value')}
            for a in product.get('attributes', []) if a.get('name') != 'Название WB'
            and a.get('id') != aid and not a.get('meta', {}).get('href', '').endswith('/' + aid)}


def verify(before, after, wanted, aid):
    if target_value(after, aid) != wanted:
        raise RuntimeError('WB name did not save')
    for field in set(before) | set(after):
        if field not in ('meta', 'updated', 'attributes') and before.get(field) != after.get(field):
            raise RuntimeError('Unrelated product field changed: ' + field)
    if other_attributes(before, aid) != other_attributes(after, aid):
        raise RuntimeError('Unrelated custom attribute changed')


def send_once(payload):
    """Exactly one HTTP attempt: uncertain writes are never retried automatically."""
    request = urllib.request.Request(ms_api.BASE + '/entity/product', method='POST',
        data=json.dumps(payload, ensure_ascii=False).encode(), headers={
            'Authorization': 'Bearer ' + ms_api.TOKEN, 'Accept-Encoding': 'gzip',
            'Content-Type': 'application/json'})
    # Webhooks deliberately remain enabled; no DisableByPrefix header is sent.
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            data = gzip.decompress(data)
        return json.loads(data) if data else None


def prepare():
    if (OUT / 'authorization.json').exists() or (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Do not overwrite authorization/execution')
    source = json.loads((PREFLIGHT / 'plan.json').read_text())
    if sha(PREFLIGHT / 'plan.json') != (PREFLIGHT / 'plan.sha256').read_text().strip():
        raise RuntimeError('Preflight checksum mismatch')
    items = source['items']
    if len(items) != 200 or len({r['ms_id'] for r in items}) != 200:
        raise RuntimeError('Pilot must contain exactly the approved 200 cards')
    for file, expected in source['source_code_sha256'].items():
        if sha(ROOT / file) != expected:
            raise RuntimeError('Formula changed since preflight')
    if not DIMENSION_LOCK.is_file() or DIMENSION_LOCK.is_symlink():
        raise RuntimeError('Confirmed dimensions/weight lock is unavailable')
    timers = check_timer_conflicts(unit_inventory('timers'))
    save('plan.json', {'items': items, 'target_attribute_id': source['target_attribute_id']})
    save('authorization.json', {'authorized': True,
        'instruction': 'поставь на 21:00 по МСК обновление 200 карточек, проверь чтобы запись размеров и веса из другой сессии не встала в это же время',
        'scope': 'Only these 200 cards; no automatic continuation',
        'start_msk': '2026-10-15 21:00', 'write_cutoff_msk': '2026-10-15 21:20',
        'hard_stop_msk': '2026-10-15 21:45', 'time_exception': '21:00 explicitly requested by user',
        'plan_sha256': sha(OUT / 'plan.json'), 'rules_version': RULES_VERSION,
        'code_sha256': {f: sha(ROOT / f) for f in CODE_FILES},
        'notification_mode': 'all_webhooks_enabled', 'TC_field_consumption_verified': False,
        'TC_polling_recovery_verified': False, 'original_print_template_verified': False,
        'calculated_label_preview_approved_by_user': True,
        'coordination_lock': str(DIMENSION_LOCK), 'timer_inventory': timers})
    save('summary.json', {'status': 'prepared_not_scheduled', 'cards': 200, 'attempted': 0,
        'verified': 0, 'skipped': 0, 'already_desired': 0, 'ms_writes_at_preparation': 0,
        'start_msk': '2026-10-15 21:00', 'other_dimensions_weight_msk': '2026-10-15 23:20',
        'write_cutoff_msk': '2026-10-15 21:20', 'hard_stop_msk': '2026-10-15 21:45'})


def check_files():
    auth = load('authorization.json')
    if auth.get('authorized') is not True or auth.get('notification_mode') != 'all_webhooks_enabled':
        raise RuntimeError('Pilot is not authorized')
    if sha(OUT / 'plan.json') != auth['plan_sha256']:
        raise RuntimeError('Frozen plan changed')
    for name, expected in auth['code_sha256'].items():
        if sha(ROOT / name) != expected:
            raise RuntimeError('Frozen execution code changed: ' + name)
    plan = load('plan.json')
    if len(plan['items']) != 200 or len({r['ms_id'] for r in plan['items']}) != 200:
        raise RuntimeError('Scope changed')
    return plan


def apply():
    plan = check_files()
    if (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Existing journal; audit required before any resume')
    summary = load('summary.json')
    guard(); baseline_latency = probe(); guard()
    journal('started', cards=200, operational_latency_seconds=baseline_latency)
    summary['status'] = 'running'; save('summary.json', summary)
    aid = plan['target_attribute_id']
    for start in range(0, 200, 50):
        guard(); check_files()
        batch = plan['items'][start:start + 50]
        codes = sorted({r['external_code'] for r in batch})
        catalog = {r['external_code']: r['raw'] for r in db.query(
            'SELECT external_code, raw FROM prc_tc_model WHERE gone_at IS NULL AND external_code = ANY(%s)', (codes,))}
        black = blacklist.load_set()
        before, payload = {}, []
        for offset in range(0, len(batch), 20):
            group = batch[offset:offset + 20]
            ids = {r['ms_id'] for r in group}
            response = ms_api.get('/entity/product', {'filter': ';'.join('id=' + r['ms_id'] for r in group), 'limit': 20})
            lives = {p['id']: p for p in response.get('rows', [])}
            if set(lives) != ids or response.get('meta', {}).get('size') != len(ids):
                raise RuntimeError('Incomplete live read before write')
            for row in group:
                live = lives[row['ms_id']]
                try:
                    body = patch(live, row, catalog.get(row['external_code']), aid, black)
                except RuntimeError as exc:
                    journal('skipped', id=row['ms_id'], reason=str(exc)); summary['skipped'] += 1
                    continue
                if body is None:
                    journal('already_desired', id=row['ms_id']); summary['already_desired'] += 1
                else:
                    before[row['ms_id']] = live; payload.append(body)
        save(f'before_{start:03}.json.gz', list(before.values()))
        save(f'payload_{start:03}.json.gz', payload)
        if payload:
            latency = probe(); guard(); check_files()
            if summary['attempted'] + len(payload) > 200:
                raise RuntimeError('Attempt limit exceeded')
            journal('send_intent', ids=[r['id'] for r in payload], batch=start)
            summary['attempted'] += len(payload); save('summary.json', summary)
            sent_at = time.monotonic()
            response = send_once(payload)
            send_latency = time.monotonic() - sent_at
            save(f'response_{start:03}.json.gz', response)
            journal('response_received', batch=start, latency_seconds=round(send_latency, 3))
            wanted = {r['ms_id']: r['new'] for r in batch}
            for body in payload:
                after = ms_api.get('/entity/product/' + body['id'])
                save('after_' + body['id'] + '.json.gz', after)
                verify(before[body['id']], after, wanted[body['id']], aid)
                journal('verified', id=body['id']); summary['verified'] += 1
            save('summary.json', summary)
            if start < 150:
                for _ in range(4 if send_latency > 10 else 2):
                    time.sleep(30); guard()
    journal('canary_pause_started', verified=summary['verified'])
    summary['status'] = 'canary_pause'; save('summary.json', summary)
    for _ in range(20):
        time.sleep(30); guard(writing=False)
    final_latency = probe(); guard(writing=False)
    if summary['verified'] + summary['skipped'] + summary['already_desired'] != 200:
        raise RuntimeError('Final count mismatch')
    journal('canary_health_check_passed', operational_latency_seconds=final_latency,
            webhooks_delivery_not_proven=True, automatic_continuation=False)
    summary.update(status='pilot_complete_no_continuation', completed_at_utc=datetime.now(timezone.utc).isoformat(),
                   operational_latency_seconds=final_latency, webhooks_delivery_not_proven=True)
    save('summary.json', summary)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare', action='store_true'); group.add_argument('--check', action='store_true')
    group.add_argument('--apply', action='store_true'); args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as stack:
            for path in [OUT / 'lock', DIMENSION_LOCK]:
                if path == DIMENSION_LOCK and not path.is_file():
                    raise RuntimeError('Dimensions/weight coordination lock missing')
                handle = stack.enter_context(path.open('a'))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.prepare:
                prepare()
            elif args.check:
                plan = check_files(); timers = check_timer_conflicts(unit_inventory('timers'))
                print(json.dumps({'cards':len(plan['items']), 'timers':timers, 'active_writers':active_writers(unit_inventory('services')), 'ms_writes':0}))
            else:
                apply()
    except Exception as exc:
        if args.apply and (OUT / 'summary.json').exists():
            summary = load('summary.json')
            if summary.get('status') != 'pilot_complete_no_continuation':
                summary.update(status='stopped_requires_audit', error_type=type(exc).__name__)
                save('summary.json', summary)
        print('Stopped safely: ' + type(exc).__name__, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# поток: infra
"""Recover only the approved, unstarted 200 WB + 605 dimension/weight edits."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import db, ms_api
from prices import blacklist
from tools import card_dimensions_weight_night as dims
from tools.prc import wb_name_canary as wb

OUT = ROOT / 'docs/reports/infra_ms_recovery_2026-10-16'
WB_OLD = wb.OUT
DIM_OLD = dims.OUT
START = datetime(2026, 10, 16, 22, 30, tzinfo=timezone.utc)
WRITE_END = START + timedelta(hours=1, minutes=30)
END = START + timedelta(hours=2)
MIN_FREE = 2 * 1024**3
CODE = tuple(dict.fromkeys((*wb.CODE_FILES, 'core/ms_api.py',
    'tools/infra_ms_recovery_20261016.py', 'tools/card_dimensions_weight_night.py',
    'tools/card_dimensions_own_source_night.py', 'tools/card_dimensions_source_inventory.py')))


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    if path.suffix == '.gz':
        data = gzip.compress(data)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def journal(event, **fields):
    with (OUT / 'journal.jsonl').open('a') as f:
        f.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(),
                            'event': event, **fields}, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())


def attrs(product):
    return {a.get('id') or a.get('meta', {}).get('href', '').rsplit('/', 1)[-1] or a.get('name'):
            a.get('value') for a in product.get('attributes', [])}


def combine(before, row, raw, aid, black):
    """Apply dimension changes before WB patch so its full attribute list retains them."""
    body = {'id': row['id']}
    if row.get('dimensions'):
        body.update(dims.patch(before, row['dimensions'], black) or {})
    intermediate = deepcopy(before)
    intermediate.update(deepcopy(body))
    if row.get('wb'):
        body.update(wb.patch(intermediate, row['wb'], raw, aid, black) or {})
    return body if len(body) > 1 else None


def verify(before, after, body):
    expected = dict(before, **body)
    for field in set(expected) | set(after):
        if field not in ('meta', 'updated', 'attributes') and expected.get(field) != after.get(field):
            raise RuntimeError('Unexpected product field after write: ' + field)
    if attrs(expected) != attrs(after):
        raise RuntimeError('Custom attributes differ from exact intended payload')


def scope(plan):
    rows = plan['items']
    ids = [r['id'] for r in rows]
    if len(ids) != 802 or len(set(ids)) != 802:
        raise RuntimeError('Expected exactly 802 unique approved cards')
    if sum(bool(r.get('wb')) for r in rows) != 200 or sum(bool(r.get('dimensions')) for r in rows) != 605:
        raise RuntimeError('Approved field scopes changed')
    if not all(r.get('wb') for r in rows[:200]) or any(r.get('wb') for r in rows[200:]):
        raise RuntimeError('WB pilot must be first, with no continuation of WB scope')
    return rows


def catalog(rows):
    codes = sorted({r['wb']['external_code'] for r in rows if r.get('wb')})
    if not codes:
        return {}
    return {r['external_code']: r['raw'] for r in db.query(
        'SELECT external_code, raw FROM prc_tc_model WHERE gone_at IS NULL AND external_code = ANY(%s)', (codes,))}


def read_products(rows):
    result = {}
    for n in range(0, len(rows), 20):
        ids = {r['id'] for r in rows[n:n+20]}
        answer = ms_api.get('/entity/product', {'limit': 20, 'filter': ';'.join('id='+i for i in sorted(ids))})
        found = {p['id']: p for p in answer.get('rows', [])}
        if set(found) != ids or answer.get('meta', {}).get('size') != len(ids):
            raise RuntimeError('Incomplete fresh product read')
        result.update(found)
    return result


def source_check(plan):
    dims.OUT = OUT
    good, skipped = dims.check_sources([r['dimensions'] for r in plan['items'] if r.get('dimensions')],
                                      plan['dimension_source_sha256'])
    return {r['id'] for r in good}, skipped


def check_files():
    auth = load(OUT / 'authorization.json')
    if auth.get('authorized') is not True or auth.get('plan_sha256') != sha(OUT / 'plan.json'):
        raise RuntimeError('Authorization or frozen plan changed')
    for path, wanted in auth['code_sha256'].items():
        if sha(ROOT / path) != wanted:
            raise RuntimeError('Frozen code changed: ' + path)
    plan = load(OUT / 'plan.json')
    scope(plan)
    return plan


def guard(writing=True, now=None):
    now = now or datetime.now(timezone.utc)
    if not START <= now < (WRITE_END if writing else END):
        raise RuntimeError('Outside authorized recovery night window')
    if shutil.disk_usage(OUT).free < MIN_FREE:
        raise RuntimeError('Less than 2 GiB free for durable backup and normal operations')
    writers = wb.active_writers(wb.unit_inventory('services'))
    if writers:
        raise RuntimeError('Other mass writer active: ' + ', '.join(writers))
    timer_conflicts()


def timer_conflicts():
    original = wb.START, wb.END
    try:
        wb.START, wb.END = START, END
        return wb.check_timer_conflicts(wb.unit_inventory('timers'))
    finally:
        wb.START, wb.END = original


def wait(seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        guard(False)
        time.sleep(max(0, min(30, until - time.monotonic())))


def send_once(payload, has_wb, prefix):
    headers = {'Authorization': 'Bearer ' + ms_api.TOKEN, 'Accept-Encoding': 'gzip',
               'Content-Type': 'application/json'}
    if not has_wb:
        headers[dims.previous.HEADER] = prefix
    request = urllib.request.Request(ms_api.BASE + '/entity/product', method='POST',
        data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            data = gzip.decompress(data)
        answer = json.loads(data)
    if not isinstance(answer, list) or len(answer) != len(payload) or {r.get('id') for r in answer} != {r['id'] for r in payload}:
        raise RuntimeError('Uncertain write response; no retry')


def prepare():
    if (OUT / 'authorization.json').exists() or (OUT / 'journal.jsonl').exists():
        raise RuntimeError('Do not overwrite a prepared recovery')
    timer_conflicts()
    for root in [WB_OLD, DIM_OLD]:
        if (root / 'journal.jsonl').exists() or list(root.glob('backup_batch*')):
            raise RuntimeError('Old run has write artifacts; individual audit required')
    wa, da = load(WB_OLD/'authorization.json'), load(DIM_OLD/'authorization.json')
    if not wa.get('authorized') or not da.get('authorized'):
        raise RuntimeError('Original instructions unavailable')
    if sha(WB_OLD/'plan.json') != wa['plan_sha256'] or sha(DIM_OLD/'plan.json') != da['plan_sha256']:
        raise RuntimeError('Original plan changed')
    for path, expected in wa['code_sha256'].items():
        if sha(ROOT/path) != expected:
            raise RuntimeError('Previously approved WB formula changed')
    wp, dp = load(WB_OLD/'plan.json'), load(DIM_OLD/'plan.json')
    by_dim = {r['id']: r for r in dp}
    rows = [{'id': r['ms_id'], 'wb': r, 'dimensions': by_dim.get(r['ms_id'])} for r in wp['items']]
    used = {r['id'] for r in rows}
    rows += [{'id': r['id'], 'dimensions': r} for r in dp if r['id'] not in used]
    plan = {'items': rows, 'wb_attribute_id': wp['target_attribute_id'],
            'dimension_source_sha256': da['source_sha256']}
    scope(plan)
    good, skipped = source_check(plan)
    raws, products, black = catalog(rows), read_products(rows), blacklist.load_set()
    checks = []
    for row in rows:
        try:
            if row.get('dimensions') and row['id'] not in good:
                raise RuntimeError('Supplier weight source changed')
            body = combine(products[row['id']], row, raws.get(row.get('wb', {}).get('external_code')),
                           plan['wb_attribute_id'], black)
            state = 'READY' if body else 'ALREADY_DESIRED'
        except (RuntimeError, ValueError) as exc:
            row['hold'] = str(exc)
            state = 'HOLD'
        checks.append({'id': row['id'], 'status': state, 'reason': row.get('hold')})
    save(OUT/'plan.json', plan)
    save(OUT/'preflight_before.json.gz', list(products.values()))
    save(OUT/'preflight_checks.json', checks)
    save(OUT/'source_skipped_preflight.json', skipped)
    save(OUT/'authorization.json', {'authorized': True,
         'instruction': 'Займись в первую очередь Базой пульта, маханизмом пульта, ночные обновления МС и журналы и обслуживание восстанови работу.',
         'scope': 'Only original 200 WB and 605 dimension/weight cards, 3 overlaps merged; no new cards',
         'start_msk': '2026-10-17 01:30', 'write_cutoff_msk': '2026-10-17 03:00', 'hard_stop_msk': '2026-10-17 03:30',
         'plan_sha256': sha(OUT/'plan.json'), 'code_sha256': {f: sha(ROOT/f) for f in CODE},
         'wb_webhooks': 'all enabled, including three merged cards',
         'dimensions_only_webhooks': 'original exact TC request prefix; no subscription mutations',
         'TC_polling_recovery_verified': False, 'original_print_template_verified': False,
         'user_authorized_bounded_recovery': True})
    state = {'status': 'prepared_not_scheduled', 'cards': 802, 'wb_cards': 200, 'dimension_cards': 605,
             'overlap': 3, 'attempted': 0, 'verified': 0, 'skipped': 0, 'already_desired': 0,
             'wb_attempted': 0, 'wb_verified': 0, 'wb_skipped': 0, 'wb_already_desired': 0,
             'dimension_attempted': 0, 'dimension_verified': 0, 'dimension_skipped': 0, 'dimension_already_desired': 0,
             'start_msk': '2026-10-17 01:30', 'hard_stop_msk': '2026-10-17 03:30',
             'preflight_ready': sum(r['status']=='READY' for r in checks),
             'preflight_hold': sum(r['status']=='HOLD' for r in checks)}
    save(OUT/'summary.json', state)
    print(json.dumps({k:v for k,v in state.items() if k in ['status','cards','overlap','preflight_ready','preflight_hold']}))


def publish(state):
    save(OUT/'summary.json', state)
    for root, prefix, count in [(WB_OLD, 'wb', 200), (DIM_OLD, 'dimension', 605)]:
        old = load(root/'summary.json')
        old.update(status=state['status'], cards=count, start_msk=state['start_msk'],
            hard_stop_msk=state['hard_stop_msk'], write_cutoff_msk='2026-10-17 03:00',
            attempted=state[prefix+'_attempted'], verified=state[prefix+'_verified'],
            skipped=state[prefix+'_skipped'], already_desired=state[prefix+'_already_desired'],
            recovery_report=str(OUT.relative_to(ROOT)), other_dimensions_weight_msk='2026-10-17 01:30')
        if prefix == 'dimension':
            old['writes'] = state['dimension_attempted']
        save(root/'summary.json', old)


def apply():
    plan = check_files()
    if (OUT/'journal.jsonl').exists():
        raise RuntimeError('Existing execution journal; no automatic retry')
    guard(); wb.probe()
    good, skipped_sources = source_check(plan)
    save(OUT/'source_skipped_runtime.json', skipped_sources)
    prefix = dims.previous.checked_prefix()
    state = load(OUT/'summary.json'); state['status'] = 'running'
    journal('started', cards=802)
    publish(state)
    rows = scope(plan)
    for offset in range(0, len(rows), 50):
        guard(); check_files()
        batch_started = time.monotonic()
        batch = rows[offset:offset+50]
        raws, black = catalog(batch), blacklist.load_set()
        before = read_products(batch)
        payload, sent_rows = [], []
        for row in batch:
            try:
                if row.get('hold'):
                    raise RuntimeError('Held at fresh preflight')
                if row.get('dimensions') and row['id'] not in good:
                    raise RuntimeError('Source changed since approval')
                body = combine(before[row['id']], row, raws.get((row.get('wb') or {}).get('external_code')),
                               plan['wb_attribute_id'], black)
            except (RuntimeError, ValueError) as exc:
                journal('skipped', id=row['id'], reason=str(exc))
                state['skipped'] += 1
                for key, field in [('wb','wb'),('dimension','dimensions')]:
                    if row.get(field): state[key+'_skipped'] += 1
                continue
            if body:
                payload.append(body); sent_rows.append(row)
            else:
                state['already_desired'] += 1
                for key, field in [('wb','wb'),('dimension','dimensions')]:
                    if row.get(field): state[key+'_already_desired'] += 1
        save(OUT/f'backup_batch_{offset:04}.json.gz', list(before.values()))
        if payload:
            save(OUT/f'payload_batch_{offset:04}.json.gz', payload)
            guard()
            has_wb = any(r.get('wb') for r in sent_rows)
            if not has_wb and dims.previous.checked_prefix() != prefix:
                raise RuntimeError('Webhook consumer changed')
            journal('send_intent', ids=[r['id'] for r in payload], wb_webhooks_enabled=has_wb)
            state['attempted'] += len(payload)
            for row in sent_rows:
                for key, field in [('wb','wb'),('dimension','dimensions')]:
                    if row.get(field): state[key+'_attempted'] += 1
            publish(state)
            send_once(payload, has_wb, prefix)
            after = read_products(sent_rows)
            save(OUT/f'after_batch_{offset:04}.json.gz', list(after.values()))
            for row, body in zip(sent_rows, payload):
                verify(before[row['id']], after[row['id']], body)
                journal('verified', id=row['id'])
                state['verified'] += 1
                for key, field in [('wb','wb'),('dimension','dimensions')]:
                    if row.get(field): state[key+'_verified'] += 1
        publish(state)
        if offset == 150:
            state['status'] = 'canary_pause'; publish(state)
            wait(600); wb.probe()
            state['status'] = 'running'; publish(state)
        elif offset + 50 < len(rows):
            wait(120 if time.monotonic() - batch_started > 30 else 60)
    state['status'] = 'canary_pause'; publish(state)
    wait(600); wb.probe()
    state.update(status='complete', completed_at=datetime.now(timezone.utc).isoformat(),
                 webhooks_delivery_not_proven=True)
    publish(state); journal('complete', verified=state['verified'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    for flag in ['prepare','check','apply','scheduled']:
        group.add_argument('--'+flag, action='store_true')
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for path in [OUT/'lock', WB_OLD/'lock', DIM_OLD/'lock']:
            f = stack.enter_context(path.open('a'))
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.prepare:
            prepare()
        elif args.check:
            plan = check_files(); print(json.dumps({'frozen': True, 'unique_cards': len(scope(plan))}))
        elif args.scheduled:
            check_files()
            for root in [WB_OLD, DIM_OLD]:
                save(OUT/(root.name+'_previous_summary.json'), load(root/'summary.json'))
            state = load(OUT/'summary.json');state['status'] = 'scheduled_ready';publish(state)
        else:
            try:
                apply()
            except Exception as exc:
                state = load(OUT/'summary.json')
                state.update(status='stopped_requires_audit', error_type=type(exc).__name__)
                publish(state)
                print('Stopped; no automatic retry: '+type(exc).__name__, flush=True)
                raise SystemExit(1)


if __name__ == '__main__':
    main()

# поток: mkt / WB
"""Owner-authorized capacity probe: one new unfunded inactive CPC draft, add <=50 each time."""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors.wb_ads import _token, B
from ops.wb_ad_enroll import body_for

P = Path(__file__).parent
NAME = 'TEST_API_X2_STEP50_20261109'
parser = argparse.ArgumentParser()
parser.add_argument('--execute', action='store_true')
args = parser.parse_args()
AUTH = json.loads((P / 'owner_authorization.json').read_text())
assert AUTH['account'] == 'wb_acc1' and not AUTH['activation_authorized'] and not AUTH['funding_authorized']
poolfile = P / 'test_pool_800.csv'
assert hashlib.sha256(poolfile.read_bytes()).hexdigest() == AUTH['pool_sha256']
ROWS = list(csv.DictReader(poolfile.open(encoding='utf-8-sig')))[:300]
POOL = [int(r['nm_id']) for r in ROWS]
assert len(POOL) == len(set(POOL)) == 300 and all(r['subject_id'] == '1820' and int(r['fresh_FBS_units']) > 0 for r in ROWS)
BEFORE = json.loads((P / 'campaigns_before.json').read_text())
FORBIDDEN = set(map(int, BEFORE['all_existing_campaign_ids']))
if not args.execute:
    print(json.dumps({'account': 'wb_acc1', 'create_initial_nms': 50, 'add_batches': [50] * 5,
                      'observe_totals': [50, 100, 150, 200, 250, 300], 'budget_deposit': 0,
                      'activation_calls': 0, 'existing_campaign_writes': 0, 'name': NAME}))
    raise SystemExit(0)
RAW = P / 'probe_a_raw'
RAW.mkdir(exist_ok=True)
H = {'Authorization': _token('wb_acc1'), 'Content-Type': 'application/json'}
bp = P / 'probe_a_binding.json'
binding = json.loads(bp.read_text()) if bp.exists() else None


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, z):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as f:
        f.write(json.dumps(z, ensure_ascii=False, indent=2) + '\n')
        f.flush(); os.fsync(f.fileno())
    tmp.replace(path)


def event(z):
    with (P / 'probe_a_operations.jsonl').open('a') as f:
        f.write(json.dumps({'account': 'wb_acc1', 'at_utc': now(), **z}, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())


def api(method, path, body=None, params=None):
    write = method in ['POST', 'PATCH'] or path == '/adv/v0/pause'
    if path == '/adv/v2/seacat/save-ad':
        assert method == 'POST' and binding is None and body['name'] == NAME
        assert body['nms'] == POOL[:50] and body['payment_type'] == 'cpc' and body['bid_type'] == 'manual'
    elif path == '/adv/v0/auction/nms':
        assert method == 'PATCH' and binding and len(body['nms']) == 1
        entry = body['nms'][0]
        assert int(entry['advert_id']) == binding['campaign_id'] and int(entry['advert_id']) not in FORBIDDEN
        assert set(entry['nms']) == {'add'} and 0 < len(entry['nms']['add']) <= 50
        assert set(entry['nms']['add']) <= set(POOL)
    elif path == '/adv/v0/pause':
        assert method == 'GET' and binding and int(params['id']) == binding['campaign_id']
    else:
        assert method == 'GET' and path in ['/api/advert/v2/adverts', '/adv/v1/promotion/count', '/adv/v1/budget']
        if path == '/adv/v1/budget':
            assert binding and int(params['id']) == binding['campaign_id']
    if write:
        event({'phase': 'INTENT', 'method': method, 'path': path, 'body': body, 'params': params})
    # Ambiguous writes are not retried. A resumed run reconciles the exact title/current set.
    r = requests.request(method, B + path, headers=H, json=body, params=params, timeout=90)
    try:
        obj = r.json() if r.content else None
    except ValueError:
        obj = {'non_json': r.text[:1000]}
    file = RAW / (dt.datetime.now().strftime('%H%M%S_%f') + '.json')
    save(file, {'account': 'wb_acc1', 'at_utc': now(), 'method': method, 'path': path,
               'request': body, 'params': params, 'http': r.status_code, 'response': obj})
    if write:
        event({'phase': 'RESPONSE', 'method': method, 'path': path, 'http': r.status_code, 'raw_file': str(file), 'response': obj})
    return r, obj


def details():
    for attempt in range(40):
        r, z = api('GET', '/api/advert/v2/adverts', params={'ids': str(binding['campaign_id'])})
        if r.status_code == 429:
            time.sleep(5); continue
        r.raise_for_status()
        rows = z.get('adverts') or []
        if len(rows) == 1 and int(rows[0]['id']) == binding['campaign_id'] and rows[0]['settings']['name'] == NAME:
            return rows[0]
        time.sleep(5)
    raise RuntimeError('New campaign identity not visible; no further writes until reconciled')


def inactive():
    c = details()
    if c['status'] == 9:
        r, _ = api('GET', '/adv/v0/pause', params={'id': binding['campaign_id']})
        r.raise_for_status(); time.sleep(3); c = details()
    assert c['status'] in [4, 11], 'New test campaign is not safely inactive'
    return c


def read_set(expected, before=None):
    for attempt in range(40):
        c = inactive()
        actual = {int(x['nm_id']) for x in c['nm_settings']}
        assert actual <= set(POOL), 'Unexpected own draft composition'
        if actual == set(expected):
            return c
        if before is None or actual != set(before):
            raise RuntimeError('Partial/unexpected membership: reconcile raw before retry')
        time.sleep(5)
    raise RuntimeError('Accepted addition not confirmed within WB metadata lag window')


if binding is None:
    # Reconcile any previous successful/ambiguous creation by exact unique name first.
    r, z = api('GET', '/adv/v1/promotion/count'); r.raise_for_status()
    possible = [int(c['advertId']) for g in z['adverts'] if g['status'] in [4, 9, 11]
                for c in g['advert_list'] if int(c['advertId']) not in FORBIDDEN]
    matches = []
    for i in range(0, len(possible), 50):
        r, z = api('GET', '/api/advert/v2/adverts', params={'ids': ','.join(map(str, possible[i:i + 50]))})
        r.raise_for_status()
        matches += [c for c in z.get('adverts') or [] if c['settings']['name'] == NAME]
    assert len(matches) <= 1
    if matches:
        cid = int(matches[0]['id'])
    else:
        old_journal = (P / 'probe_a_operations.jsonl').read_text() if (P / 'probe_a_operations.jsonl').exists() else ''
        assert not any('/adv/v2/seacat/save-ad' in line and 'INTENT' in line for line in old_journal.splitlines()), 'Unresolved prior creation intent; do not create another draft'
        body = {'name': NAME, 'nms': POOL[:50], 'bid_type': 'manual', 'payment_type': 'cpc', 'placement_types': ['search']}
        r, z = api('POST', '/adv/v2/seacat/save-ad', body=body)
        r.raise_for_status()
        assert isinstance(z, int) and z > 0
        cid = z
    assert cid not in FORBIDDEN
    binding = {'account': 'wb_acc1', 'campaign_id': cid, 'name': NAME, 'created_at_utc': now(),
               'pool_sha256': AUTH['pool_sha256'], 'activation_allowed': False, 'funding_allowed': False}
    save(bp, binding)
assert binding['account'] == 'wb_acc1' and binding['campaign_id'] not in FORBIDDEN
c = inactive()
r, budget = api('GET', '/adv/v1/budget', params={'id': binding['campaign_id']}); r.raise_for_status()
assert budget.get('total') == 0, 'Unexpected nonzero budget: leave inactive, no funding'
save(P / 'probe_a_budget_before.json', {'account': 'wb_acc1', 'at_utc': now(), 'campaign_id': binding['campaign_id'], 'budget': budget})
assert c['settings']['payment_type'] == 'cpc' and c['bid_type'] == 'manual', 'Requested CPC not confirmed; leave inactive and investigate'
steps = []
for target in [50, 100, 150, 200, 250, 300]:
    c = inactive()
    current = {int(x['nm_id']) for x in c['nm_settings']}
    if target == 50 or len(current) >= target:
        assert set(POOL[:target]) <= current
        steps.append({'target': target, 'verified_total': len(current), 'http': None, 'status': 'ALREADY_PRESENT'})
        continue
    assert current == set(POOL[:target - 50])
    add = POOL[target - 50:target]
    response, obj = api('PATCH', '/adv/v0/auction/nms', body=body_for(binding['campaign_id'], add=add))
    if response.status_code != 200:
        c = inactive()
        actual = {int(x['nm_id']) for x in c['nm_settings']}
        assert actual == current, 'Rejected write nevertheless changed membership; review'
        steps.append({'target': target, 'add_count': 50, 'verified_total': len(actual),
                      'http': response.status_code, 'status': 'REJECTED_UNCHANGED', 'response': obj})
        print('BATCH REJECTED', 'target', target, 'current', len(actual), 'HTTP', response.status_code, flush=True)
        break
    c = read_set(POOL[:target], before=current)
    save(P / f'probe_a_verified_{target}.json', {'account': 'wb_acc1', 'at_utc': now(), 'campaign': c})
    steps.append({'target': target, 'add_count': 50, 'verified_total': target, 'http': 200, 'status': 'VERIFIED'})
    print('BATCH VERIFIED', target, 'SKU, CPC, inactive', flush=True)
    time.sleep(2)
c = inactive()
r, budget = api('GET', '/adv/v1/budget', params={'id': binding['campaign_id']}); r.raise_for_status()
assert budget['total'] == 0
result = {'account': 'wb_acc1', 'finished_at_utc': now(), 'method': 'create50_then_PATCH_add50', 'campaign_id': binding['campaign_id'],
          'payment_type': c['settings']['payment_type'], 'bid_type': c['bid_type'], 'campaign_state': c['status'],
          'verified_SKU_count': len(c['nm_settings']), 'steps': steps, 'budget': budget,
          'activation_calls': 0, 'deposit_calls': 0, 'existing_campaign_writes': 0}
save(P / 'probe_a_result.json', result)
save(P / 'probe_a_final_readback.json', {'account': 'wb_acc1', 'at_utc': now(), 'campaign': c})
print('CAPACITY PROBE COMPLETE', binding['campaign_id'], len(c['nm_settings']), 'CPC inactive; budget0; no starts/deposits', flush=True)

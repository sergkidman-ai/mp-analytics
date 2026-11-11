# поток: mkt / WB
"""Compare UI-like client-side precreate selection with postcreate add50; no spend."""
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

P = Path(__file__).parent
parser = argparse.ArgumentParser()
parser.add_argument('--execute', action='store_true')
args = parser.parse_args()
if not args.execute:
    print('DRY_RUN: precreate200 assembled in client-side batches50; 300 only after verified200; no API calls')
    raise SystemExit(0)
auth = json.loads((P / 'owner_authorization.json').read_text())
assert auth['account'] == 'wb_acc1' and not auth['activation_authorized'] and not auth['funding_authorized']
assert hashlib.sha256((P / 'test_pool_800.csv').read_bytes()).hexdigest() == auth['pool_sha256']
rows = list(csv.DictReader((P / 'test_pool_800.csv').open(encoding='utf-8-sig')))
forbidden = set(map(int, json.loads((P / 'campaigns_before.json').read_text())['all_existing_campaign_ids']))
header = {'Authorization': _token('wb_acc1'), 'Content-Type': 'application/json'}
RAW = P / 'precreate_raw'
RAW.mkdir(exist_ok=True)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, z):
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as f:
        f.write(json.dumps(z, ensure_ascii=False, indent=2) + '\n')
        f.flush(); os.fsync(f.fileno())
    temp.replace(path)


def journal(z):
    with (P / 'precreate_operations.jsonl').open('a') as f:
        f.write(json.dumps({'account': 'wb_acc1', 'at_utc': now(), **z}, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())


def request(method, path, body=None, params=None):
    assert (method == 'POST' and path == '/adv/v2/seacat/save-ad') or (method == 'GET' and path in ['/api/advert/v2/adverts', '/adv/v1/budget'])
    if method == 'POST':
        assert body['payment_type'] == 'cpc' and body['bid_type'] == 'manual'
        assert body['name'] in ['TEST_API_X2_PRECREATE200_20261109', 'TEST_API_X2_PRECREATE300_20261109']
        journal({'phase': 'INTENT', 'method': method, 'path': path, 'body': body})
    response = requests.request(method, B + path, headers=header, json=body, params=params, timeout=90)
    try:
        z = response.json() if response.content else None
    except ValueError:
        z = {'non_json': response.text[:1000]}
    save(RAW / (dt.datetime.now().strftime('%H%M%S%f') + '.json'), {'account': 'wb_acc1', 'at_utc': now(),
         'method': method, 'path': path, 'request': body, 'params': params, 'http': response.status_code, 'response': z})
    if method == 'POST':
        journal({'phase': 'RESPONSE', 'method': method, 'path': path, 'http': response.status_code, 'response': z})
    return response, z


results = []
for count, start in [(200, 300), (300, 500)]:
    name = f'TEST_API_X2_PRECREATE{count}_20261109'
    selected = [int(r['nm_id']) for r in rows[start:start + count]]
    draft = []
    stages = []
    for i in range(0, count, 50):
        draft.extend(selected[i:i + 50])
        stages.append(len(draft))
    assert len(draft) == len(set(draft)) == count
    body = {'name': name, 'nms': draft, 'bid_type': 'manual', 'payment_type': 'cpc', 'placement_types': ['search']}
    marker = P / f'precreate_{count}_intent_marker.json'
    bindingfile = P / f'precreate_{count}_binding.json'
    resultfile = P / f'precreate_{count}_result.json'
    if resultfile.exists():
        prior = json.loads(resultfile.read_text())
        results.append(prior)
        if prior.get('state') != 'FULL_COUNT_VERIFIED':
            break
        continue
    assert not marker.exists(), 'Prior creation may have completed; reconcile exact unique title before any retry'
    save(marker, {'account': 'wb_acc1', 'at_utc': now(), 'draft_selection_stages_client_side_only': stages,
                   'single_final_API_request': True, 'payload': body})
    response, z = request('POST', '/adv/v2/seacat/save-ad', body=body)
    if response.status_code != 200:
        result = {'account': 'wb_acc1', 'requested_count': count, 'selection_stages_client_side_only': stages,
                  'http': response.status_code, 'state': 'CREATE_REJECTED', 'response': z,
                  'activation_calls': 0, 'deposit_calls': 0}
        save(resultfile, result); results.append(result)
        print('PRECREATE REJECTED', count, 'HTTP', response.status_code, flush=True)
        break
    assert isinstance(z, int) and z > 0 and z not in forbidden
    cid = z
    save(bindingfile, {'account': 'wb_acc1', 'campaign_id': cid, 'name': name, 'created_at_utc': now(),
                      'activation_allowed': False, 'funding_allowed': False})
    latest = None
    for attempt in range(40):
        r, obj = request('GET', '/api/advert/v2/adverts', params={'ids': str(cid)})
        r.raise_for_status()
        got = [c for c in obj.get('adverts') or [] if int(c['id']) == cid and c['settings']['name'] == name]
        if got:
            latest = got[0]
            assert latest['status'] in [4, 11] and latest['settings']['payment_type'] == 'cpc' and latest['bid_type'] == 'manual'
            actual = {int(n['nm_id']) for n in latest['nm_settings']}
            assert actual <= set(selected)
            if actual == set(selected):
                break
        time.sleep(5)
    assert latest is not None
    r, budget = request('GET', '/adv/v1/budget', params={'id': cid}); r.raise_for_status(); assert budget['total'] == 0
    result = {'account': 'wb_acc1', 'requested_count': count, 'selection_stages_client_side_only': stages,
              'http': 200, 'state': 'FULL_COUNT_VERIFIED' if len(latest['nm_settings']) == count else 'PARTIAL_COUNT_ONLY',
              'campaign_id': cid, 'payment_type': latest['settings']['payment_type'], 'campaign_status': latest['status'],
              'verified_count': len(latest['nm_settings']), 'budget': budget, 'activation_calls': 0, 'deposit_calls': 0}
    save(resultfile, result); save(P / f'precreate_{count}_readback.json', {'account': 'wb_acc1', 'at_utc': now(), 'campaign': latest})
    results.append(result)
    print('PRECREATE VERIFIED', count, 'requested,', len(latest['nm_settings']), 'actual, CPC inactive, budget0', flush=True)
    if result['state'] != 'FULL_COUNT_VERIFIED':
        break
    time.sleep(13)
save(P / 'precreate_results.json', results)

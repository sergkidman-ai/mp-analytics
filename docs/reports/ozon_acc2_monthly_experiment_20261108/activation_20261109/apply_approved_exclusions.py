# поток: mkt / ozon
"""Owner-approved exact 19 prelaunch removals from four inactive acc2 campaigns only."""
import collections
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
from collectors import ozon_ads as ads, ozon_bids

P = Path(__file__).parent
LAUNCH = P.parent
AUTH = json.loads((P / 'launch_authorization.json').read_text())
assert AUTH['account'] == 'oz_acc2' and AUTH['composition_change_authorized'] and AUTH['launch_authorized']
assert AUTH['authorized_deferred_skus'] == 19
BINDING_PATH = LAUNCH / 'create_wave1/created_campaign_bindings.json'
ORIGINAL = json.loads(BINDING_PATH.read_text())
assert ORIGINAL['account'] == 'oz_acc2' and ORIGINAL['all_four_inactive_verified']
IDS = {'50080606', '50080615', '50080620', '50080623'}
assert {x['campaign_id'] for x in ORIGINAL['campaigns']} == IDS
loadcsv = lambda name: list(csv.DictReader((P / name).open(encoding='utf-8-sig')))
READY = loadcsv('READY_3851_NOT_APPLIED.csv')
DEFERRED = loadcsv('DEFERRED_19_NOT_APPLIED.csv')
assert len(READY) == len({r['sku'] for r in READY}) == 3851
assert len(DEFERRED) == len({r['sku'] for r in DEFERRED}) == 19
assert all(r['account'] == 'oz_acc2' and r['actual_campaign_id'] in IDS for r in READY + DEFERRED)
assert not {r['sku'] for r in READY} & {r['sku'] for r in DEFERRED}
OUT = P / 'exclusions_applied'
OUT.mkdir(exist_ok=True)
H = {'Authorization': 'Bearer ' + ads._token('oz_acc2')}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, obj):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def journal(obj):
    with (OUT / 'operations.jsonl').open('a') as f:
        f.write(json.dumps({'account': 'oz_acc2', 'at_utc': now(), **obj}, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def campaigns():
    for attempt in range(8):
        r = requests.get(ads.PERF + '/api/client/campaign', headers=H, timeout=90)
        if r.status_code != 429:
            break
        time.sleep(min(15, max(3, float(r.headers.get('Retry-After') or 5))))
    r.raise_for_status()
    return {str(c['id']): c for c in r.json()['list']}


before = campaigns()
if not (OUT / 'campaigns_before.json').exists():
    save(OUT / 'campaigns_before.json', {'account': 'oz_acc2', 'at_utc': now(), 'campaigns': list(before.values())})
assert all(before[cid]['state'] == 'CAMPAIGN_STATE_INACTIVE' for cid in IDS)
new_binding = json.loads(json.dumps(ORIGINAL))
new_binding.update(source_original_bindings_sha256=hashlib.sha256(BINDING_PATH.read_bytes()).hexdigest(),
                   owner_exclusions_authorization=str(P / 'launch_authorization.json'),
                   uploaded_skus=3851, deferred_skus=19, membership_version='PRELAUNCH_OWNER_APPROVED_19_EXCLUSIONS')
results = []
for c in new_binding['campaigns']:
    cid = c['campaign_id']
    assert cid in IDS and cid not in set(ORIGINAL['forbidden_existing_campaign_ids'])
    wanted = {r['sku'] for r in READY if r['actual_campaign_id'] == cid}
    excluded = {r['sku'] for r in DEFERRED if r['actual_campaign_id'] == cid}
    original = json.loads((BINDING_PATH.parent / c['readback_file']).read_text())
    assert wanted | excluded == {str(x['sku']) for x in original['products']}
    products, code = ozon_bids._campaign_products(H, cid)
    assert code == 200
    actual = {str(x['sku']) for x in products}
    assert wanted <= actual <= wanted | excluded, 'Unexpected composition; reconcile before writing'
    pre = OUT / f'before_{cid}.json'
    if not pre.exists():
        save(pre, {'account': 'oz_acc2', 'at_utc': now(), 'campaign': before[cid], 'products': products})
    remaining = sorted(actual & excluded)
    if remaining:
        path = f'/api/client/campaign/{cid}/products/delete'
        body = {'sku': remaining}
        assert set(body['sku']) <= excluded and set(body['sku']) & wanted == set()
        journal({'phase': 'INTENT', 'method': 'POST', 'path': path, 'body': body})
        # Ambiguous POST is not blindly retried; rerun reconciles exact current membership above.
        response = requests.post(ads.PERF + path, headers=H, json=body, timeout=90)
        result = response.json() if response.content else None
        raw = OUT / f'remove_response_{cid}_{dt.datetime.now():%H%M%S%f}.json'
        save(raw, {'account': 'oz_acc2', 'at_utc': now(), 'request': body, 'path': path,
                   'http': response.status_code, 'response': result})
        journal({'phase': 'RESPONSE', 'method': 'POST', 'path': path, 'http': response.status_code, 'raw_file': str(raw)})
        response.raise_for_status()
    products, code = ozon_bids._campaign_products(H, cid)
    assert code == 200 and {str(x['sku']) for x in products} == wanted
    current = campaigns()[cid]
    assert current['state'] == 'CAMPAIGN_STATE_INACTIVE' and current['title'] == c['title']
    rb = P / f'readback_at_start_{cid}.json'
    save(rb, {'account': 'oz_acc2', 'at_utc': now(), 'campaign': current, 'products': products})
    c.update(uploaded_skus=len(products), own_eligible_skus=len(products), readback_file=rb.name,
             verified_state=current['state'], verified_at_utc=now(), verified_complete=True,
             prelaunch_owner_approved_deferred_skus=len(excluded))
    results.append({'campaign_id': cid, 'ready': len(wanted), 'excluded': len(excluded), 'state': current['state']})
    print('EXCLUSIONS VERIFIED', cid, 'remaining', len(products), 'deferred', len(excluded), flush=True)
new_binding.update(finished_at_utc=now(), all_four_inactive_verified=True)
new_binding_path = P / 'bindings_at_start.json'
save(new_binding_path, new_binding)
after = campaigns()
fields = ['state', 'title', 'weeklyBudget', 'dailyBudget', 'budget', 'productAutopilotStrategy']
old_differences = [{'id': cid, 'field': k, 'before': c.get(k), 'after': after[cid].get(k)}
                   for cid, c in before.items() if cid not in IDS for k in fields if c.get(k) != after[cid].get(k)]
save(OUT / 'existing_campaign_integrity.json', {'account': 'oz_acc2', 'differences': old_differences,
     'writes_scope': sorted(IDS), 'bonus_49322606_writes': 0})
assert not old_differences, 'An existing campaign changed externally; review before launch'
cfg_path = LAUNCH / 'risk_policy.json'
cfg = json.loads(cfg_path.read_text())
assert cfg['account'] == 'oz_acc2' and cfg['activation_allowed'] is False
mp = P / 'frozen_family_maps_at_start.json'
cfg.update(bindings_path=str(new_binding_path), bindings_sha256=hashlib.sha256(new_binding_path.read_bytes()).hexdigest(),
           family_maps_path=str(mp), family_maps_sha256=hashlib.sha256(mp.read_bytes()).hexdigest(),
           owner_authorized_wave1_activation=True, activation_blocker=None)
save(cfg_path, cfg)
save(OUT / 'result.json', {'account': 'oz_acc2', 'finished_at_utc': now(), 'campaigns': results,
     'total_remaining_skus': 3851, 'total_deferred_skus': 19, 'activation_calls': 0,
     'old_campaign_write_calls': 0, 'immutable_original_binding_sha256': hashlib.sha256(BINDING_PATH.read_bytes()).hexdigest()})
print('EXCLUSIONS COMPLETE: exact19 deferred, 3851 verified; four inactive; original signed binding retained')

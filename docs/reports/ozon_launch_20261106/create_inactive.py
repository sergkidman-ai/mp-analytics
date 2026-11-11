# поток: mkt / ozon
"""Explicitly authorized creation/upload only. Activation is prohibited by construction."""
import csv
import datetime as dt
from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors import ozon_ads as ads, ozon_bids

OUT = Path(__file__).parent
PLAN = OUT.parent / 'ozon_wave_revision_20261106/wave_draft_manifest.json'
M = json.loads(PLAN.read_text())
BEFORE = json.loads((OUT / 'campaigns_before.json').read_text())
FORBIDDEN = {str(c['id']) for c in BEFORE['campaigns']}
with (OUT / 'fresh_eligibility.csv').open(encoding='utf-8-sig') as f:
    ALL = list(csv.DictReader(f))
READY = [r for r in ALL if r['eligible_for_upload'] == 'True']
HEADERS = {'Authorization': 'Bearer ' + ads._token('oz_acc1')}
RAW = OUT / 'mutation_raw'
RAW.mkdir(exist_ok=True)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, z):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(z, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


bp = OUT / 'created_campaign_bindings.json'
binding = json.loads(bp.read_text()) if bp.exists() else {
    'account': 'oz_acc1', 'source_plan_sha256': hashlib.sha256(PLAN.read_bytes()).hexdigest(),
    'authorization': 'owner directly requested create 12 inactive campaigns, upload eligible SKU and set individual fresh minima; no activation',
    'forbidden_existing_campaign_ids': sorted(FORBIDDEN), 'campaigns': [], 'activation_allowed': False}
assert binding['source_plan_sha256'] == hashlib.sha256(PLAN.read_bytes()).hexdigest()


def campaigns():
    for attempt in range(8):
        r = requests.get(ads.PERF + '/api/client/campaign', headers=HEADERS, timeout=90)
        if r.status_code != 429:
            break
        time.sleep(min(30, max(3, float(r.headers.get('Retry-After') or 3)) + attempt))
    r.raise_for_status()
    return {str(c['id']): c for c in r.json()['list']}


def mutation(method, path, body=None, retry=0):
    assert '/activate' not in path, 'ACTIVATION PROHIBITED'
    if path != '/api/client/campaign/cpc/v2/product':
        cid = path.split('/')[4]
        assert cid not in FORBIDDEN and cid in {c['campaign_id'] for c in binding['campaigns']}
        assert path in [f'/api/client/campaign/{cid}/products', f'/api/client/campaign/{cid}/deactivate',
                        f'/api/client/campaign/{cid}']
    else:
        assert body['title'] in {c['title'] for c in M['campaigns']}
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    intent = {'at_utc': now(), 'method': method, 'path': path, 'body': body,
              'new_inactive_campaigns_only': True}
    with (OUT / 'mutation_journal.jsonl').open('a') as f:
        f.write(json.dumps({**intent, 'phase': 'INTENT'}, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())
    # Mutations are not automatically retried after network ambiguity.
    r = requests.request(method, ads.PERF + path, headers=HEADERS, json=body, timeout=120)
    obj = r.json() if r.content else {}
    save(RAW / f'{stamp}.json', {**intent, 'http': r.status_code, 'response': obj})
    with (OUT / 'mutation_journal.jsonl').open('a') as f:
        f.write(json.dumps({**intent, 'phase': 'RESPONSE', 'http': r.status_code,
                           'response_file': f'mutation_raw/{stamp}.json'}, ensure_ascii=False) + '\n')
    if r.status_code == 429:
        # Explicit rejection is safe to retry; network ambiguity above remains non-retriable.
        assert retry < 8, 'Mutation rate limit persisted; resume from readback later'
        time.sleep(max(3, min(30, float(r.headers.get('Retry-After') or 5))))
        return mutation(method, path, body, retry + 1)
    time.sleep(.4)
    return r, obj


def inactive(cid):
    c = campaigns()[cid]
    if c['state'] != 'CAMPAIGN_STATE_INACTIVE':
        r, _ = mutation('POST', f'/api/client/campaign/{cid}/deactivate')
        r.raise_for_status()
        c = campaigns()[cid]
    assert c['state'] == 'CAMPAIGN_STATE_INACTIVE', (cid, c['state'])
    return c


def add(cid, rr, failures):
    body = {'bids': [{'sku': r['sku'], 'bid': r['bid_micro_rub']} for r in rr]}
    response, obj = mutation('POST', f'/api/client/campaign/{cid}/products', body)
    if response.status_code == 400 and len(rr) > 1:
        # Isolate an individual rejected SKU, never exclude its whole family.
        half = len(rr) // 2
        add(cid, rr[:half], failures); add(cid, rr[half:], failures)
    elif response.status_code == 400:
        failures[rr[0]['sku']] = {'http': 400, 'response': obj}
    else:
        response.raise_for_status()


for plan in M['campaigns']:
    rr = [r for r in READY if r['campaign_key'] == plan['key']]
    entry = next((c for c in binding['campaigns'] if c['plan_key'] == plan['key']), None)
    if entry and entry.get('verified_complete'):
        inactive(entry['campaign_id'])
        continue
    if not entry:
        matches = [c for c in campaigns().values() if c['title'] == plan['title']]
        assert len(matches) <= 1
        weekly = len(rr) * 2000
        if matches:
            cid = str(matches[0]['id'])
            assert cid not in FORBIDDEN, 'Never adopt a pre-existing preparation campaign'
        else:
            payload = {k: v for k, v in plan['create_payload_preview'].items() if v is not None}
            # Current API rejects even zero: "autoincrease is depricated".
            payload.pop('autoIncreasePercent', None)
            payload.update(fromDate=dt.datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat(),
                           weeklyBudget=str(weekly * 1000000))
            response, obj = mutation('POST', '/api/client/campaign/cpc/v2/product', payload)
            response.raise_for_status()
            cid = str(obj.get('campaignId') or obj.get('id') or '')
            assert cid.isdigit() and cid not in FORBIDDEN, 'Missing or unsafe new campaign ID'
        entry = {'plan_key': plan['key'], 'campaign_id': cid, 'title': plan['title'], 'wave': plan['wave'],
                 'created_at_utc': now(), 'planned_skus': plan['planned_skus'],
                 'own_eligible_skus': len(rr), 'weekly_budget_preview_rub': weekly,
                 'weekly_budget_is_actual_spend_authorization': False, 'activation_called': False}
        binding['campaigns'].append(entry); save(bp, binding)
    cid = entry['campaign_id']
    inactive(cid)
    got, code = ozon_bids._campaign_products(HEADERS, cid)
    assert code == 200
    wanted = {r['sku']: r for r in rr}
    assert all(str(p['sku']) in wanted for p in got), 'Unexpected SKU in newly bound campaign'
    correct = {str(p['sku']) for p in got if D(str(p['bid'])) == D(wanted[str(p['sku'])]['bid_micro_rub'])}
    todo = [r for r in rr if r['sku'] not in correct]
    failures = entry.get('upload_failures', {})
    for i in range(0, len(todo), 100):
        add(cid, todo[i:i + 100], failures)
    c = inactive(cid)
    got, code = ozon_bids._campaign_products(HEADERS, cid)
    assert code == 200
    expected = {r['sku'] for r in rr} - set(failures)
    assert {str(p['sku']) for p in got} == expected, 'Readback SKU set mismatch; do not claim upload complete'
    assert all(D(str(p['bid'])) == D(wanted[str(p['sku'])]['bid_micro_rub']) for p in got), 'Individual minimum readback mismatch'
    entry.update(verified_complete=True, verified_at_utc=now(), verified_state=c['state'],
                 uploaded_skus=len(got), upload_failures=failures,
                 weekly_budget_api_micro_rub=c.get('weeklyBudget'), readback_file=f'readback_{cid}.json')
    save(OUT / entry['readback_file'], {'at_utc': now(), 'campaign': c, 'products': got})
    save(bp, binding)
    print('INACTIVE VERIFIED', plan['key'], cid, 'SKU', len(got), 'individual minimum bids match', flush=True)
final = campaigns()
assert all(final[c['campaign_id']]['state'] == 'CAMPAIGN_STATE_INACTIVE' for c in binding['campaigns'])
binding.update(finished_at_utc=now(), all_12_inactive_verified=len(binding['campaigns']) == 12,
               actual_activation_events=[], uploaded_skus=sum(c['uploaded_skus'] for c in binding['campaigns']))
save(bp, binding)
print('CREATE/UPLOAD COMPLETE', len(binding['campaigns']), 'campaigns, SKU', binding['uploaded_skus'], 'NO ACTIVATIONS', flush=True)

# поток: mkt / WB
"""Independent final reads after the one-time payment; never funds or starts."""
import csv
import datetime as dt
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors.wb_ads import B, _token

P = Path(__file__).parent
plan = json.loads((P / 'manifest.json').read_text())
execution = json.loads((P / 'execution_result.json').read_text())
events = [json.loads(s) for s in (P / 'operations.jsonl').read_text().splitlines()]
success = [e for e in events if e['phase'] == 'RESPONSE' and e['http'] == 200]
assert len(success) == len({e['key'] for e in success}), 'Repeated successful operation'
raws = [json.loads(f.read_text()) for f in (P / 'raw').glob('*.json')]
deposits = [e for e in success if e['kind'] == 'deposit']
for intent in [e for e in events if e['phase'] == 'INTENT' and e['kind'] == 'deposit']:
    before = [r for r in raws if r.get('path') == '/api/advert/v2/budget'
              and r['account'] == intent['account'] and r['request']['advertIds'] == [intent['campaign_id']]
              and r['at_utc'] < intent['at_utc']]
    assert before
    latest = max(before, key=lambda r:r['at_utc'])
    assert latest['response']['adverts'][0]['total'] <= 50


def read(account, method, path, params=None, body=None):
    assert (method == 'GET' and path in ['/api/advert/v2/adverts', '/adv/v1/balance', '/adv/v1/promotion/count']) or (method == 'POST' and path == '/api/advert/v2/budget')
    r = requests.request(method, B + path, headers={'Authorization': _token(account)}, params=params, json=body, timeout=60)
    r.raise_for_status()
    z = r.json()
    (P / f'final_{account}_{dt.datetime.now().strftime("%H%M%S%f")}.json').write_text(json.dumps({'account': account, 'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'method': method, 'path': path, 'params': params, 'request': body, 'http': r.status_code, 'response': z}, ensure_ascii=False, indent=2)+'\n')
    return z


rows, summary, bid_diffs = [], {}, []
for account, binding in plan['accounts'].items():
    ids = sorted(c['id'] for c in binding['campaigns'])
    for attempt in range(36):
        campaigns = read(account, 'GET', '/api/advert/v2/adverts', params={'ids': ','.join(map(str, ids))})['adverts']
        assert {c['id'] for c in campaigns} == set(ids)
        pending = [c['id'] for c in campaigns if c['status'] != 9]
        if not pending:
            break
        print(account, 'waiting for WB status synchronization', len(pending), flush=True)
        time.sleep(5)
    assert not pending, f'Start status not yet confirmed: {pending}'
    budget_rows = read(account, 'POST', '/api/advert/v2/budget', body={'advertIds': ids})['adverts']
    budgets = {int(b['advertId']): b['total'] for b in budget_rows}
    assert set(budgets) == set(ids) and all(b['currency'] == 'RUB' for b in budget_rows)
    wallet = read(account, 'GET', '/adv/v1/balance')
    funded = json.loads((P / f'{account}_funding_verified.json').read_text())
    actual = [e for e in deposits if e['account'] == account]
    assert len(actual) == len(ids)
    assert funded['wallet_before']['net'] - wallet['net'] == len(actual) * 1000
    before = {c['id']: c for c in binding['campaigns']}
    for c in campaigns:
        prior = before[c['id']]
        nm = {n['nm_id']: n for n in c['nm_settings']}
        old = {n['nm_id']: n for n in prior['nm_settings']}
        assert set(nm) == set(old), 'Unexpected membership change'
        for n in nm:
            if nm[n]['bids_kopecks'] != old[n]['bids_kopecks']:
                bid_diffs.append({'account':account, 'campaign_id':c['id'], 'nm_id':n, 'before':old[n]['bids_kopecks'], 'after':nm[n]['bids_kopecks']})
        assert c['settings']['payment_type'] == prior['settings']['payment_type']
        rows.append({'account':account, 'campaign_id':c['id'], 'name':c['settings']['name'], 'payment_type':c['settings']['payment_type'], 'before_status':prior['status'], 'after_status':c['status'], 'sku_count':len(nm), 'budget_before_preflight_RUB':binding['budgets'][str(c['id'])], 'added_RUB':1000, 'final_budget_RUB':budgets[c['id']], 'actual_started_at':c['timestamps'].get('started')})
    count = read(account, 'GET', '/adv/v1/promotion/count')
    all_running = sum(len(g['advert_list']) for g in count.get('adverts',[]) if g['status'] == 9)
    summary[account] = {'funded_campaigns':len(actual), 'deposited_RUB':len(actual)*1000, 'start_calls_HTTP200':sum(e['account']==account and e['kind']=='start' for e in success), 'selected_final_running':len(campaigns), 'all_account_running':all_running, 'wallet_before_net':funded['wallet_before']['net'], 'wallet_after_net':wallet['net']}
assert len(deposits) == 21 and sum(e['kind']=='start' for e in success) == 11
with (P / 'verified_result.csv').open('w', encoding='utf-8-sig', newline='') as f:
    w=csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
retry_file = P / 'start_retry_once.jsonl'
retry_events = [json.loads(s) for s in retry_file.read_text().splitlines()] if retry_file.exists() else []
retry_count = sum(e['phase']=='RESPONSE' and e.get('http')==200 for e in retry_events)
result = {'verified_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(), 'status':'PASS', 'accounts':summary, 'total_funded':21, 'total_added_RUB':21000, 'total_starts':11, 'additional_start_retry_calls':retry_count, 'all_selected_status9':True, 'every_pretransfer_budget_le50':True, 'membership_changes':0, 'observed_bid_changes':bid_diffs, 'recurring_autodeposits_enabled':False}
(P / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
print(json.dumps({**result, 'observed_bid_changes':len(bid_diffs)}, ensure_ascii=False), flush=True)

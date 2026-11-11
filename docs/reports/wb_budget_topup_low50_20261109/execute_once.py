# поток: mkt / WB
"""One authorized operation: add1000 only to fixed <=50 campaigns, then start standing.

Uses existing deposit/start commands. Every monetary intent is fsynced before HTTP.
No automatic retry of a write, including after an ambiguous response.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors.wb_ads import B, _token
from ops.wb_ad_campaign_new import deposit, start

P = Path(__file__).parent


def save(path, obj):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
        f.flush(); os.fsync(f.fileno())
    tmp.replace(path)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def journal(obj):
    with (P / 'operations.jsonl').open('a') as f:
        f.write(json.dumps({'at_utc': now(), **obj}, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())


def validate_write(plan, kind, account, cid, method, url, params, body):
    candidates = {int(c['id']): c for c in plan['accounts'][account]['campaigns']}
    assert cid in candidates
    assert plan['accounts'][account]['budgets'][str(cid)] <= 50
    assert params == {'id': cid}
    if kind == 'deposit':
        assert method.upper() == 'POST' and url == B + '/adv/v1/budget/deposit'
        assert body == {'sum': 1000, 'type': 1, 'return': True}
    else:
        assert kind == 'start' and cid in plan['accounts'][account]['start_ids']
        assert candidates[cid]['settings']['payment_type'] == 'cpc'
        assert method.upper() == 'GET' and url == B + '/adv/v0/start' and body is None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true')
    args = ap.parse_args()
    manifest = P / 'manifest.json'
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == (P / 'manifest.sha256').read_text().strip()
    plan = json.loads(manifest.read_text())
    assert plan['threshold_RUB'] == 50 and plan['add_RUB'] == 1000 and plan['one_time_only']
    assert plan['total_RUB'] == 21000
    if not args.execute:
        print('DRY_RUN:21 fixed <=50 campaigns, add1000 each,total21000;11CPCstarts;no network calls')
        return
    if (P / 'verification.json').exists() and json.loads((P / 'verification.json').read_text()).get('status') == 'PASS':
        raise SystemExit('Already completed and verified; use the saved report or verify_readonly.py; no repeat execution')
    lock = (P / 'execution.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    raw = P / 'raw'; raw.mkdir(exist_ok=True)
    statefile = P / 'state.json'
    state = json.loads(statefile.read_text()) if statefile.exists() else {'wallet_before': {}, 'skipped': {}, 'source_verified': False}
    events = [json.loads(s) for s in (P / 'operations.jsonl').read_text().splitlines()] if (P / 'operations.jsonl').exists() else []
    done = {e['key'] for e in events if e['phase'] == 'RESPONSE' and e['http'] == 200}
    last_budget_read = {}

    def read(account, method, path, params=None, body=None):
        assert (method == 'GET' and path in ['/adv/v1/balance', '/api/advert/v2/adverts']) or (method == 'POST' and path == '/api/advert/v2/budget')
        if path == '/api/advert/v2/budget':
            time.sleep(max(0, 3.2 - (time.monotonic() - last_budget_read.get(account, 0))))
            last_budget_read[account] = time.monotonic()
        r = requests.request(method, B + path, headers={'Authorization': _token(account)}, params=params, json=body, timeout=60)
        try: z = r.json()
        except ValueError: z = {'non_json': r.text[:1000]}
        save(raw / (dt.datetime.now().strftime('%H%M%S%f') + '.json'), {'at_utc': now(), 'account': account, 'method': method, 'path': path, 'params': params, 'request': body, 'http': r.status_code, 'response': z})
        r.raise_for_status()
        return z

    def budgets(account, ids):
        z = read(account, 'POST', '/api/advert/v2/budget', body={'advertIds': ids})
        assert len(z['adverts']) == len(ids)
        assert {int(b['advertId']) for b in z['adverts']} == set(ids)
        assert all(b['currency'] == 'RUB' for b in z['adverts'])
        return {int(b['advertId']): b['total'] for b in z['adverts']}

    def perform(kind, account, cid):
        key = f'{account}:{kind}:{cid}'
        if key in done:
            return
        assert not (P / 'CANCELLED_BY_USER.json').exists(), 'Cancelled by owner'
        assert not any(e['key'] == key for e in events), 'Prior write attempt exists; reconcile, never retry blindly'
        intent = {'phase': 'INTENT', 'key': key, 'account': account, 'kind': kind, 'campaign_id': cid, 'amount_RUB': 1000 if kind == 'deposit' else None}
        journal(intent); events.append(intent)
        original = requests.sessions.Session.request
        calls = []

        def capture(session, method, url, **kw):
            validate_write(plan, kind, account, cid, method, url, kw.get('params'), kw.get('json'))
            assert kw['headers']['Authorization'] == _token(account) and not calls
            calls.append(True)
            r = original(session, method, url, **kw)
            try: response = r.json() if r.content else None
            except ValueError: response = {'non_json': r.text[:1000]}
            payload = {'phase': 'RESPONSE', 'key': key, 'account': account, 'kind': kind, 'campaign_id': cid, 'http': r.status_code, 'response': response}
            save(raw / (dt.datetime.now().strftime('%H%M%S%f') + '.json'), {'at_utc': now(), 'method': method, 'path': url.removeprefix(B), 'params': kw.get('params'), 'request': kw.get('json'), **payload})
            journal(payload); events.append(payload)
            if r.status_code == 200:
                done.add(key)
            return r

        requests.sessions.Session.request = capture
        try:
            code, _ = deposit(account, cid, 1000, src=1) if kind == 'deposit' else start(account, cid)
        finally:
            requests.sessions.Session.request = original
        assert code == 200, f'{key} HTTP{code}; stop without retry'
        print(kind.upper(), account, cid, 'HTTP200', flush=True)
        time.sleep(1.6)

    for account, binding in plan['accounts'].items():
        if account not in state['wallet_before']:
            state['wallet_before'][account] = read(account, 'GET', '/adv/v1/balance')
            save(statefile, state)
        selected = sorted(c['id'] for c in binding['campaigns'])
        # First transfer is to a paused CPC campaign, enabling exact source verification.
        if account == 'wb_acc1':
            pilot = binding['start_ids'][0]
            selected = [pilot] + [cid for cid in selected if cid != pilot]
        for cid in selected:
            key = f'{account}:deposit:{cid}'
            if key in done or str(cid) in state['skipped'].get(account, {}):
                continue
            before = budgets(account, [cid])[cid]
            if before > 50:
                state['skipped'].setdefault(account, {})[str(cid)] = {'reason': 'CURRENT_BUDGET_GT50', 'budget': before}
                save(statefile, state)
                continue
            if not state['source_verified']:
                wallet_before = read(account, 'GET', '/adv/v1/balance')
            perform('deposit', account, cid)
            if not state['source_verified']:
                after = budgets(account, [cid])[cid]
                wallet_after = read(account, 'GET', '/adv/v1/balance')
                assert wallet_before['net'] - wallet_after['net'] == 1000, 'Funding source must be verified before any more transfers'
                assert after - before == 1000, 'Pilot paused campaign must gain exactly1000'
                state['source_verified'] = True
                save(P / 'source_verification.json', {'account': account, 'campaign_id': cid, 'wallet_before': wallet_before, 'wallet_after': wallet_after, 'campaign_before': before, 'campaign_after': after})
                save(statefile, state)
        wallet_after = read(account, 'GET', '/adv/v1/balance')
        count = sum(f'{account}:deposit:{cid}' in done for cid in selected)
        assert state['wallet_before'][account]['net'] - wallet_after['net'] == 1000 * count, 'Wallet debit does not match the exact authorized transfers'
        save(P / f'{account}_funding_verified.json', {'at_utc': now(), 'count': count, 'amount_RUB': count * 1000, 'wallet_before': state['wallet_before'][account], 'wallet_after': wallet_after, 'budgets_after': budgets(account, sorted(selected))})

    for account, binding in plan['accounts'].items():
        ids = [cid for cid in binding['start_ids'] if f'{account}:deposit:{cid}' in done]
        if not ids:
            continue
        current = read(account, 'GET', '/api/advert/v2/adverts', params={'ids': ','.join(map(str, ids))})['adverts']
        assert {c['id'] for c in current} == set(ids)
        for c in current:
            assert c['settings']['payment_type'] == 'cpc'
            if c['status'] in [4, 11]:
                perform('start', account, c['id'])
            else:
                assert c['status'] == 9, 'Unexpected status, do not restart archived/finished campaigns'

    save(P / 'execution_result.json', {'at_utc': now(), 'deposit_count': sum(':deposit:' in k for k in done), 'deposited_RUB': 1000 * sum(':deposit:' in k for k in done), 'start_calls_HTTP200': sum(':start:' in k for k in done), 'skipped': state['skipped'], 'automatic_future_deposits_enabled': False, 'independent_campaign_readback_required': True})
    print('EXECUTION COMPLETE; independent budget/status readback required', flush=True)


if __name__ == '__main__':
    main()

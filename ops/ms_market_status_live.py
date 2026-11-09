# поток: inv
"""Rolling 90-day extension of ms_market_status_sync; never writes to marketplaces."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

import requests
from core import db, ms_api
from shipment_guard.config import identity, ORGS
from ops.fbs_ms_post_import_shipments import durable_save
from ops.ms_market_status_sources import Provider, SUPPORTED, canonical
from ops import ms_market_status_state as state

MSK = ZoneInfo('Europe/Moscow')
MAX_FAST = 40


def now():
    return datetime.now(timezone.utc)


def aware(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=MSK) if parsed.tzinfo is None else parsed


def window(at=None):
    local = (at or now()).astimezone(MSK)
    start = (local - timedelta(days=90)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def in_window(document, at=None):
    try:
        start, end = window(at)
        return start <= aware(document['moment']).astimezone(MSK) < end
    except (KeyError, TypeError, ValueError):
        return False


def night_id(at=None):
    local = (at or now()).astimezone(MSK)
    if not (local.hour >= 23 or local.hour < 5):
        return None
    return (local.date() - timedelta(days=local.hour < 5)).isoformat()


def status_ids():
    cached = state.setting('status_ids')
    if cached and time.time() - cached['at'] < 86400:
        return cached['ids']
    data = ms_api.get('/entity/customerorder/metadata')
    result = {}
    for name in ('В пути', *state.TARGETS.values()):
        matching = [r for r in data['states'] if r['name'] == name]
        if len(matching) != 1:
            raise ValueError('ms_status_metadata_ambiguous_' + name)
        result[name] = matching[0]['id']
    state.set_setting('status_ids', {'at': time.time(), 'ids': result})
    return result


def href(uid):
    return ms_api.BASE + '/entity/customerorder/metadata/states/' + uid


def row_for(document):
    platform, account, *_ = identity(document)
    if (platform, account) not in SUPPORTED or not in_window(document):
        return None
    store = document.get('store', {}).get('name', '')
    hint = 'fbo' if any(s in store.upper() for s in ('FBO', 'FBW', 'FBY', 'СКЛАД WB')) else 'fbs'
    return {'id': document['id'], 'platform': platform, 'account': account,
            'number': document['name'], 'moment': document['moment'],
            'updated': document.get('updated', ''), 'store': store, 'scheme_hint': hint}


def scan_ms(folder, updated_since=None):
    start, end = window()
    filters = ['state=' + href(status_ids()['В пути']),
               'moment>=' + start.strftime('%Y-%m-%d %H:%M:%S'),
               'moment<' + end.strftime('%Y-%m-%d %H:%M:%S')]
    if updated_since:
        filters.append('updated>=' + aware(updated_since).astimezone(MSK).strftime('%Y-%m-%d %H:%M:%S'))
    rows, seen, offset = [], set(), 0
    while True:
        data = ms_api.get('/entity/customerorder', {'limit': 100, 'offset': offset,
                          'filter': filters, 'expand': 'state,store'})
        durable_save(folder / ('ms_page_' + str(offset) + '.json'), data)
        page, size = data.get('rows', []), data.get('meta', {}).get('size')
        if not isinstance(size, int) or not page and offset < size:
            raise ValueError('ms_scope_incomplete')
        for document in page:
            if document['id'] in seen:
                raise ValueError('ms_scope_duplicate_id')
            seen.add(document['id'])
            row = row_for(document)
            if row and document.get('state', {}).get('name') == 'В пути':
                rows.append(row)
        offset += len(page)
        if offset >= size:
            break
        if offset >= 20000:
            raise ValueError('ms_scope_page_limit')
    durable_save(folder / 'scope.json', {'at': now().isoformat(), 'from': start.isoformat(),
                 'to': end.isoformat(), 'rows': rows, 'excluded': len(seen) - len(rows)})
    state.index(rows)
    return rows


def poll_pult(baseline=False):
    cursor = state.setting('pult_cursor')
    since = aware(cursor) - timedelta(minutes=2) if cursor and not baseline else datetime(2000, 1, 1, tzinfo=timezone.utc)
    rows = db.query('''SELECT id,platform,account,posting_number,mp_status,observed_at,
      mp_payload->>'scheme' scheme,mp_payload->'market_order'->>'programType' program
      FROM customer_guard_orders WHERE observed_at>=%s ORDER BY observed_at,id''', (since,))
    count, latest = 0, cursor
    for row in rows:
        if (row['platform'], row['account']) not in SUPPORTED:
            continue
        if row.get('program') not in (None, 'FBS', 'FBY') or row.get('scheme') not in (None, 'fbs', 'fbo', 'fbw'):
            continue
        scheme = 'fbo' if row.get('scheme') == 'fbo' or row.get('program') == 'FBY' else 'fbs'
        proof = {'platform': row['platform'], 'account': row['account'], 'number': row['posting_number'],
                 'scheme': scheme, 'status': canonical(row['platform'], row['mp_status']),
                 'detail': row['mp_status'], 'fetched_at': row['observed_at'].isoformat(),
                 'evidence': 'customer_guard_orders:' + str(row['id'])}
        count += state.enqueue(proof, 'fast', baseline=baseline)
        latest = max(latest or proof['fetched_at'], proof['fetched_at'])
    if latest:
        state.set_setting('pult_cursor', latest)
    return {'observed': len(rows), 'enqueued': count, 'baseline': baseline}


def cached_observation(row):
    key = '|'.join(row[k] for k in ('platform', 'account', 'number'))
    with state.database() as con:
        old = con.execute('SELECT payload FROM observations WHERE key=?', (key,)).fetchone()
    return json.loads(old['payload']) if old else None


def collect(rows, folder, kind, force=False):
    provider = Provider(folder / 'api')
    fresh, unresolved = [], []
    for row in rows:
        proof = cached_observation(row) if kind == 'fast' else None
        if proof and (now() - aware(proof['fetched_at'])).total_seconds() <= 600:
            proof = dict(proof, ms_id=row['id'])
            state.enqueue(proof, kind, force=force)
        else:
            fresh.append(row)
    results = provider.fetch(fresh)
    for row in fresh:
        proof = results.get(row['id'], {'error': 'status_not_returned'})
        if 'error' in proof:
            unresolved.append({'id': row['id'], 'number': row['number'], **proof})
        else:
            state.learn_scheme(row['id'], proof['scheme'])
            state.enqueue(proof, kind, force=force)
    durable_save(folder / 'sources.json', {'results': results, 'unverified': unresolved})
    return unresolved


def find_document(proof):
    indexed = [row for row in state.documents(proof['platform'], proof['account'], proof['number']) if in_window(row)]
    if len(indexed) == 1:
        return indexed[0]['id']
    orgs = [uid for uid, item in ORGS.items() if str(item[0]) == proof['account'][-1]]
    if len(orgs) != 1:
        raise ValueError('account_organization_ambiguous')
    start, end = window()
    data = ms_api.get('/entity/customerorder', {'limit': 2, 'expand': 'state,store', 'filter': [
        'name=' + proof['number'], 'organization=' + ms_api.BASE + '/entity/organization/' + orgs[0],
        'state=' + href(status_ids()['В пути']), 'moment>=' + start.strftime('%Y-%m-%d %H:%M:%S'),
        'moment<' + end.strftime('%Y-%m-%d %H:%M:%S')]})
    if data.get('meta', {}).get('size', len(data['rows'])) != len(data['rows']):
        raise ValueError('ms_external_id_ambiguous')
    found = [o for o in data['rows'] if o['name'] == proof['number']
             and identity(o)[:2] == (proof['platform'], proof['account'])]
    if len(found) > 1:
        raise ValueError('ms_external_id_ambiguous')
    if found:
        state.index([row_for(found[0])])
    return found[0]['id'] if found else None


def operational_health():
    for unit in ('fbs-ms-import.timer', 'ms-supply-receiver.service', 'ms-supply-fulfillment.service'):
        result = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise RuntimeError('operational_unit_not_active_' + unit)
    ms_api.get('/entity/customerorder', {'limit': 1})


def canary(night):
    if state.night_confirmed(night) < 200:
        return True
    key = 'canary_' + night
    saved = state.setting(key)
    if not saved:
        state.set_setting(key, {'until': time.time() + 600, 'done': False})
        return False
    if saved['done']:
        return True
    if time.time() < saved['until']:
        return False
    operational_health()
    state.set_setting(key, dict(saved, done=True))
    return True


def apply_job(job, night=None):
    proof = job['proof']
    if proof.get('status') not in state.TARGETS:
        return {'result': 'unchanged'}
    uid = find_document(proof)
    if not uid:
        return {'result': 'outside_ms_scope'}
    if job['kind']=='urgent' and proof.get('ms_id')!=uid:
        return {'result':'urgent_ms_identity_changed'}
    # Pace first, then make the fresh MS check immediately before the write.
    slot = state.reserve_slot()
    while slot > time.time():
        time.sleep(min(slot - time.time(), 30))
    document = ms_api.get('/entity/customerorder/' + uid, {'expand': 'state,store'})
    if (document.get('name') != proof['number'] or document.get('id') != uid
            or identity(document)[:2] != (proof['platform'], proof['account']) or not in_window(document)):
        return {'result': 'identity_or_window_changed'}
    if job['kind']=='urgent':
        fresh=Provider(state.RUNTIME/'urgent_verify'/uid).fetch([
            dict(row_for(document),verified_scheme=proof['scheme'])]).get(uid,{})
        if 'error' in fresh or not fresh.get('status'):
            raise RuntimeError('urgent_cancellation_unverified')
        if fresh['status']!='cancelled':return {'result':'cancellation_not_confirmed'}
        proof=dict(fresh,ms_id=uid)
    target = state.TARGETS[proof['status']]
    if document.get('state', {}).get('name') != 'В пути':
        state.retire(uid)
        return {'result': 'already_updated' if document.get('state', {}).get('name') == target else 'changed_by_other'}
    if (now() - aware(proof['fetched_at'])).total_seconds() > 600:
        fresh = cached_observation(row_for(document))
        if not fresh or (now() - aware(fresh['fetched_at'])).total_seconds() > 600:
            fresh = Provider(state.RUNTIME / 'refresh' / uid).fetch([dict(row_for(document), verified_scheme=proof['scheme'])]).get(uid, {})
        if 'error' in fresh or not fresh.get('status'):
            raise RuntimeError('market_status_refresh_unverified')
        fresh = dict(fresh, ms_id=uid)
        state.learn_scheme(uid, fresh['scheme'])
        state.enqueue(fresh, job['kind'], force=True)
        return {'result': 'refreshed', 'retry_after': 1}
    if not state.current(job) or night and not night_id():
        return {'result': 'scope_changed_or_night_ended', 'retry_after': 30}
    attempt, created = state.begin_attempt(document, target, proof, night)
    if not created:
        return {'result': 'uncertain', 'reason': 'prior_intent_requires_fact_reconciliation', 'attempt': attempt['id']}
    folder = state.RUNTIME / 'attempts' / attempt['id']
    payload = {'state': {'meta': {'href': href(status_ids()[target]), 'type': 'state', 'mediaType': 'application/json'}}}
    durable_save(folder / 'intent.json', {'at': now().isoformat(), 'before': document, 'target': target, 'proof': proof, 'payload': payload})
    result = {}
    started = time.monotonic()
    try:
        response = requests.put(ms_api.BASE + '/entity/customerorder/' + uid,
            headers={'Authorization': 'Bearer ' + ms_api.TOKEN, 'Content-Type': 'application/json', 'Accept-Encoding': 'gzip'},
            params={'expand': 'state'}, json=payload, timeout=(5, 45))
        result['http'] = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {'unparsed_response': True}
        durable_save(folder / 'response.json', {'http': response.status_code, 'body': body})
    except requests.RequestException as exc:
        result['transport_error'] = type(exc).__name__
    # A timeout always resolves through a factual read, never another PUT.
    try:
        actual = ms_api.get('/entity/customerorder/' + uid, {'expand': 'state'})
        durable_save(folder / 'readback.json', actual)
        confirmed = (actual.get('id') == uid and actual.get('name') == proof['number']
                     and identity(actual)[:2] == (proof['platform'], proof['account'])
                     and actual.get('state', {}).get('name') == target)
    except Exception as exc:
        confirmed = False
        result['readback_error'] = type(exc).__name__
    rejected=not confirmed and result.get('http')==429 and not result.get('transport_error')
    result.update(result='updated' if confirmed else 'rate_limited' if rejected else 'uncertain', number=proof['number'], id=uid, target=target)
    if rejected:
        from shipment_guard.ms import retry_seconds
        result['retry_after']=max(3,retry_seconds(response.headers))
    state.latency(time.monotonic() - started)
    state.end_attempt(attempt['id'], 'confirmed' if confirmed else 'rejected' if rejected else 'uncertain', result)
    if confirmed:
        state.retire(uid)
    durable_save(folder / 'result.json', result)
    return result


def drain(kind, limit):
    results = []
    night = night_id() if kind == 'night' else None
    for _ in range(limit):
        if kind == 'night' and (not night_id() or not canary(night)):
            break
        if kind == 'night':
            with state.database() as con:
                if con.execute('SELECT count(*) FROM attempts WHERE night=?', (night,)).fetchone()[0] >= 10000:
                    break
        job = state.claim(kind)
        if not job:
            break
        try:
            result = apply_job(job, night)
            state.finish(job, result, retry_after=result.get('retry_after'), uncertain=result.get('result') == 'uncertain')
        except Exception as exc:
            result = {'result': 'error', 'reason': type(exc).__name__, 'number': job['proof']['number']}
            state.finish(job, result, retry_after=600)
        results.append(result)
    return results


def run_fast(folder):
    feed = poll_pult()
    cursor = state.setting('ms_discovery_cursor', now().isoformat())
    started = now().isoformat()
    changed = scan_ms(folder / 'changed_ms', aware(cursor) - timedelta(minutes=2))
    issues = collect(changed, folder / 'changed_sources', 'fast', force=True)
    state.set_setting('ms_discovery_cursor', started)
    changed_ids = {row['id'] for row in changed}
    fbo = [row for row in state.documents(fbo_only=True) if row['id'] not in changed_ids and in_window(row)]
    issues += collect(fbo, folder / 'fbo_sources', 'fast') if fbo else []
    return {'mode': 'fast', 'feed': feed, 'changed_ms': len(changed), 'fbo_checked': len(fbo),
            'unverified': issues, 'results': drain('fast', MAX_FAST)}


def run_night(folder):
    night = night_id()
    if not night:
        return {'mode': 'night', 'result': 'outside_night_window'}
    operational_health()
    rows = scan_ms(folder)
    issues, results = [], []
    for start in range(0, len(rows), 50):
        if not night_id():
            break
        issues += collect(rows[start:start + 50], folder / ('batch_' + str(start)), 'night', force=True)
        results += drain('night', 50)
        if not canary(night):
            while night_id() and not canary(night):
                time.sleep(30)
    # Drain saved work from previous runs as well as the frozen current scope.
    while night_id():
        if not canary(night):
            time.sleep(30)
            continue
        chunk = drain('night', 50)
        results += chunk
        if not chunk:
            with state.database() as con:
                following = con.execute("SELECT min(available) FROM jobs WHERE state='pending'").fetchone()[0]
            if following is not None and following <= time.time() + 30:
                time.sleep(max(1, min(following - time.time(), 30)))
                continue
            break
    return {'mode': 'night', 'night': night, 'candidates': len(rows), 'unverified': issues, 'results': results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('fast', 'night', 'seed', 'preview'), default='preview')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--seed-file')
    args = parser.parse_args()
    ms_api.TIMEOUT, ms_api.RETRIES = 45, 2
    state.RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state.RUNTIME / (args.mode + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        folder = state.RUNTIME / 'runs' / (now().strftime('%Y%m%dT%H%M%S') + '_' + args.mode)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.mode == 'seed':
            data = json.loads(Path(args.seed_file).read_text())
            rows = [row_for(o) for o in data['orders']]
            state.index([row for row in rows if row]);status_ids()
            if not state.setting('initialized'):
                result = poll_pult(baseline=True)
                state.set_setting('ms_discovery_cursor', data.get('at', now().isoformat()))
                state.set_setting('initialized', now().isoformat())
            else:
                result = {'baseline': False, 'already_initialized': True}
            result.update(mode='seed', ms_indexed=sum(bool(r) for r in rows))
        elif not args.apply or args.mode == 'preview':
            rows = scan_ms(folder)
            result = {'mode': 'preview', 'candidates': len(rows), 'ms_writes': 0}
        else:
            result = run_fast(folder) if args.mode == 'fast' else run_night(folder)
        result['at'] = now().isoformat()
        with state.database() as con:
            result['queue'] = dict(con.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall())
            result['manual'] = [{'number': json.loads(row['payload'])['number'],
                                 'result': json.loads(row['result']) if row['result'] else None}
                                for row in con.execute("SELECT payload,result FROM jobs WHERE state='uncertain' LIMIT 20")]
        durable_save(folder / 'result.json', result)
        durable_save(state.RUNTIME / (args.mode + '_latest.json'), result)
        counts = Counter(r.get('result') for r in result.get('results', []))
        print(json.dumps({'mode': result['mode'], 'at': result['at'], 'results': dict(counts),
                          'unverified': len(result.get('unverified', [])), 'queue': result['queue'],
                          'candidates': result.get('candidates')}, ensure_ascii=False))
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()

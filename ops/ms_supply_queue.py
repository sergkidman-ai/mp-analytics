# поток: inv
"""Durable supply notification queue; receiver never runs fulfillment inline."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
RUNTIME = ROOT / '.private/ms_supply_fulfillment'
ACCEPTED_STATE = '5c76771e-dd9b-11e2-e25d-7054d21a8d1e'


def utc(value=None):
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('Europe/Moscow'))
    return parsed.astimezone(timezone.utc).isoformat()


@contextmanager
def database(path=None):
    path = Path(path or os.getenv('MS_SUPPLY_QUEUE_PATH', str(RUNTIME / 'runtime.sqlite')))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=FULL')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS deliveries (
                account_id TEXT NOT NULL, request_id TEXT NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY(account_id,request_id));
            CREATE TABLE IF NOT EXISTS events (
                receipt_id TEXT NOT NULL,event_timestamp TEXT NOT NULL,event_id TEXT NOT NULL,
                source TEXT NOT NULL,payload TEXT NOT NULL,
                PRIMARY KEY(receipt_id,event_timestamp));
            CREATE TABLE IF NOT EXISTS jobs (
                receipt_id TEXT PRIMARY KEY,event_timestamp TEXT NOT NULL,event_id TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,available_at REAL NOT NULL,
                claim_token TEXT,lease_until REAL,result TEXT,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state,available_at);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        yield connection
    finally:
        connection.close()


def accept(account_id, request_id, events, *, source='webhook', path=None, now=None):
    moment = time.time() if now is None else now
    received_at = datetime.fromtimestamp(moment, timezone.utc).isoformat()
    accepted = 0
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            delivery = db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',
                                  (account_id, request_id, received_at))
            if not delivery.rowcount:
                db.commit()
                return {'accepted': 0, 'duplicate': True}
            for event in events:
                receipt_id = event['receipt_id']
                stamp = utc(event.get('event_timestamp') or received_at)
                event_id = str(uuid4())
                inserted = db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',
                                      (receipt_id, stamp, event_id, source,
                                       json.dumps(event, ensure_ascii=False)))
                if not inserted.rowcount:
                    continue
                existing = db.execute('SELECT event_timestamp FROM jobs WHERE receipt_id=?',
                                      (receipt_id,)).fetchone()
                if existing and existing['event_timestamp'] >= stamp:
                    continue
                db.execute('''INSERT INTO jobs(receipt_id,event_timestamp,event_id,available_at,updated_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(receipt_id) DO UPDATE SET
                    event_timestamp=excluded.event_timestamp,event_id=excluded.event_id,
                    generation=jobs.generation+1,
                    state=CASE WHEN jobs.state='running' THEN 'running' ELSE 'pending' END,
                    attempts=CASE WHEN jobs.state='running' THEN jobs.attempts ELSE 0 END,
                    available_at=excluded.available_at,updated_at=excluded.updated_at''',
                           (receipt_id, stamp, event_id, moment, received_at))
                accepted += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'accepted': accepted, 'duplicate': False}


def claim(*, path=None, now=None, lease_seconds=180):
    moment = time.time() if now is None else now
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('''SELECT * FROM jobs WHERE
            (state='pending' AND available_at<=?) OR (state='running' AND lease_until<=?)
            ORDER BY available_at,receipt_id LIMIT 1''', (moment, moment)).fetchone()
        if not row:
            db.commit()
            return None
        token = str(uuid4())
        db.execute("UPDATE jobs SET state='running',claim_token=?,lease_until=?,attempts=attempts+1 WHERE receipt_id=?",
                   (token, moment + lease_seconds, row['receipt_id']))
        db.commit()
        return {**dict(row), 'claim_token': token, 'attempts': row['attempts'] + 1}


def renew(job, *, path=None, lease_seconds=180):
    with database(path) as db:
        return db.execute("UPDATE jobs SET lease_until=? WHERE receipt_id=? AND claim_token=? AND state='running'",
                          (time.time() + lease_seconds, job['receipt_id'], job['claim_token'])).rowcount == 1


def finish(job, result, *, path=None, now=None):
    moment = time.time() if now is None else now
    outcome = result.get('status', 'retry')
    delay = max(0, float(result.get('delay_seconds') or 0))
    if outcome == 'retry':
        delay = max(delay, min(900, 5 * 2 ** min(job['attempts'], 8)))
    not_before = result.get('not_before')
    ready = max(moment + delay, datetime.fromisoformat(utc(not_before)).timestamp() if not_before else moment)
    state = outcome if outcome in ('done', 'manual') else 'pending'
    with database(path) as db:
        db.execute('BEGIN IMMEDIATE')
        actual = db.execute('SELECT * FROM jobs WHERE receipt_id=? AND claim_token=?',
                            (job['receipt_id'], job['claim_token'])).fetchone()
        if not actual:
            db.commit()
            return False
        if actual['generation'] != job['generation']:
            state, ready = 'pending', moment
        db.execute('''UPDATE jobs SET state=?,available_at=?,claim_token=NULL,lease_until=NULL,
                   result=?,updated_at=? WHERE receipt_id=? AND claim_token=?''',
                   (state, ready, json.dumps(result, ensure_ascii=False, default=str), utc(),
                    job['receipt_id'], job['claim_token']))
        db.commit()
    return True


def run_once(fulfill=None, *, path=None):
    job = claim(path=path)
    if not job:
        return None
    if fulfill is None:
        from ops.ms_supply_fulfillment import fulfill_receipt
        fulfill = fulfill_receipt
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(30):
            try:
                if not renew(job, path=path):
                    return
            except sqlite3.Error:
                pass

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = fulfill(job['receipt_id'], job['event_timestamp'], apply=True, event_id=job['event_id'])
        if not isinstance(result, dict) or result.get('status') not in ('done', 'continue', 'retry', 'manual'):
            result = {'status': 'retry', 'error': 'invalid_worker_result'}
    except Exception as exc:
        # Exception text may contain a URL or credential from an upstream client.
        result = {'status': 'retry', 'error': type(exc).__name__}
    finally:
        stop.set()
        thread.join(timeout=1)
    finish(job, result, path=path)
    return {'receipt_id': job['receipt_id'], 'status': result.get('status', 'retry')}


def readiness(path=None):
    with database(path) as db:
        db.execute('SELECT 1 FROM jobs LIMIT 1').fetchone()
    return True


def activate_reconciliation(activated_at=None, *, path=None):
    activated_at = utc(activated_at)
    with database(path) as db:
        db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('reconcile_activation', activated_at))
        db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('reconcile_checkpoint', activated_at))


def reconcile(*, path=None, get=None):
    """Recover missed accepted-supply pushes from an activation checkpoint only."""
    with database(path) as db:
        row = db.execute("SELECT value FROM settings WHERE key='reconcile_checkpoint'").fetchone()
        activation = db.execute("SELECT value FROM settings WHERE key='reconcile_activation'").fetchone()
    if not row:
        return {'status': 'inactive'}
    from core import ms_api
    import requests
    start = datetime.fromisoformat(row['value'])
    floor = datetime.fromisoformat(activation['value'] if activation else row['value'])
    end = datetime.now(timezone.utc)
    lower = max(floor, start - timedelta(seconds=120)).astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')
    upper = end.astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')
    fetch = get or requests.get
    offset, events = 0, []
    while True:
        response = fetch(ms_api.BASE + '/entity/supply', headers={'Authorization': 'Bearer ' + ms_api.TOKEN,
                        'Accept-Encoding': 'gzip'}, params={'limit': 20, 'offset': offset, 'order': 'updated,asc',
                        'filter': 'updated>=' + lower + ';updated<=' + upper + ';state=' +
                        ms_api.BASE + '/entity/supply/metadata/states/' + ACCEPTED_STATE}, timeout=(5, 45))
        if response.status_code != 200:
            raise RuntimeError('reconciliation_http_' + str(response.status_code))
        data = response.json()
        for supply in data.get('rows', []):
            if datetime.fromisoformat(utc(supply['updated'])) >= floor:
                events.append({'receipt_id': supply['id'], 'event_timestamp': supply['updated']})
        offset += len(data.get('rows', []))
        size = data.get('meta', {}).get('size')
        if not isinstance(size, int) or (offset < size and not data.get('rows')):
            raise RuntimeError('reconciliation_incomplete')
        if offset >= size:
            break
    result = accept('reconcile', str(uuid4()), events, source='reconcile', path=path)
    with database(path) as db:
        db.execute("UPDATE settings SET value=? WHERE key='reconcile_checkpoint'", (end.isoformat(),))
    return {'status': 'done', **result}


def register_webhook():
    """Create our UPDATE subscription once; never edit another subscription."""
    import requests
    from core import ms_api
    secret = os.environ['MS_SUPPLY_WEBHOOK_SECRET']
    public = os.environ['MS_SUPPLY_PUBLIC_BASE_URL'].rstrip('/')
    if public != 'https://89-207-255-54.sslip.io' or len(secret) < 32:
        raise RuntimeError('webhook_public_configuration_invalid')
    url = public + '/hooks/ms-supply/' + secret
    audit = RUNTIME / 'webhook_registration'
    audit.mkdir(parents=True, exist_ok=True, mode=0o700)
    audit.chmod(0o700)

    def record(name, data, exclusive=False):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
        fd = os.open(audit / name, flags, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

    try:
        health = requests.get(public + '/health', timeout=(3, 8))
        probe = requests.post(url, params={'requestId': str(uuid4())}, json={'events': []}, timeout=(3, 8))
        if health.status_code != 200 or probe.status_code != 400:
            raise RuntimeError('webhook_receiver_not_ready')
        headers = {'Authorization': 'Bearer ' + ms_api.TOKEN, 'Accept-Encoding': 'gzip'}
        endpoint = ms_api.BASE + '/entity/webhook'
        response = requests.get(endpoint, headers=headers, params={'limit': 100}, timeout=(5, 30))
        if response.status_code != 200:
            raise RuntimeError('webhook_list_unavailable')
        body = response.json()
        if body.get('meta', {}).get('size', len(body['rows'])) != len(body['rows']):
            raise RuntimeError('webhook_list_incomplete')
        matches = [r for r in body['rows'] if r.get('url') == url and r.get('entityType') == 'supply'
                   and r.get('action') == 'UPDATE']
        if len(matches) > 1 or matches and not matches[0].get('enabled'):
            raise RuntimeError('own_webhook_duplicate_or_disabled')
        existing = bool(matches)
        if existing:
            webhook = matches[0]
        else:
            if (audit / 'intent.json').exists():
                raise RuntimeError('prior_webhook_intent_requires_reconciliation')
            # Actual secret-bearing URL exists only in .env and the HTTPS request.
            record('intent.json', {'at': utc(), 'public_base': public, 'entityType': 'supply',
                                  'action': 'UPDATE', 'diffType': 'FIELDS'}, exclusive=True)
            response = requests.post(endpoint, headers=headers, json={'url': url, 'entityType': 'supply',
                                     'action': 'UPDATE', 'method': 'POST', 'enabled': True,
                                     'diffType': 'FIELDS'}, timeout=(5, 30))
            if response.status_code not in (200, 201):
                record('response.json', {'http': response.status_code})
                raise RuntimeError('webhook_create_rejected')
            webhook = response.json()
        if (webhook.get('url') != url or webhook.get('entityType') != 'supply' or
                webhook.get('action') != 'UPDATE' or not webhook.get('enabled')):
            raise RuntimeError('webhook_response_mismatch')
        result = {'id': webhook['id'], 'entityType': 'supply', 'action': 'UPDATE', 'enabled': True,
                  'public_base': public, 'existing': existing, 'at': utc()}
        record('response.json', result)
        activate_reconciliation()
        return result
    except requests.RequestException as exc:
        record('uncertain.json', {'error': type(exc).__name__, 'at': utc()})
        raise RuntimeError('webhook_network_error_no_blind_retry') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--reconcile', action='store_true')
    parser.add_argument('--register', action='store_true')
    args = parser.parse_args()
    if args.register:
        print(json.dumps(register_webhook()))
        return
    if args.reconcile:
        print(json.dumps(reconcile()))
        return
    if not args.worker:
        parser.error('--worker or --reconcile required')
    next_reconcile = time.monotonic() + 300
    while True:
        result = run_once()
        if result:
            print(json.dumps(result), flush=True)
        if args.once:
            break
        if time.monotonic() >= next_reconcile:
            try:
                reconcile()
            except Exception as exc:
                print(json.dumps({'reconcile_error': type(exc).__name__}), flush=True)
            next_reconcile = time.monotonic() + 300
        if result is None:
            time.sleep(1)


if __name__ == '__main__':
    main()

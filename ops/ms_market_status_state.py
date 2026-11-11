# поток: inv
"""Private durable state for the existing MS marketplace status reconciliation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.private/ms_market_status_continuous'
SOURCES = ('Собирается', 'В пути')
TARGETS = {'transit': 'В пути', 'delivered': 'Доставлен', 'cancelled': 'Отменен - сроки'}


@contextmanager
def database():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    path = RUNTIME / 'state.sqlite'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=FULL')
    con.executescript('''
      CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,platform TEXT,account TEXT,
        number TEXT,moment TEXT,updated TEXT,scheme TEXT,payload TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS documents_external ON documents(platform,account,number);
      CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY,stamp TEXT,status TEXT,payload TEXT);
      CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,generation INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'pending',kind TEXT,available REAL,lease TEXT,lease_until REAL,
        payload TEXT,result TEXT,updated TEXT);
      CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state,available);
      CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,ms_id TEXT,target TEXT,
        before_updated TEXT,at REAL,night TEXT,outcome TEXT,payload TEXT,result TEXT);
    ''')
    try:
        yield con
    finally:
        con.close()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def setting(key, default=None):
    with database() as con:
        row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def set_setting(key, value):
    with database() as con:
        con.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, json.dumps(value, ensure_ascii=False)))


def index(rows):
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        for row in rows:
            con.execute('''INSERT INTO documents VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              platform=excluded.platform,account=excluded.account,number=excluded.number,
              moment=excluded.moment,updated=excluded.updated,payload=excluded.payload
              WHERE excluded.updated>=documents.updated''',
              (row['id'], row['platform'], row['account'], row['number'], row['moment'],
               row.get('updated') or '', row.get('scheme_hint', 'unknown'), json.dumps(row, ensure_ascii=False)))
        con.commit()


def documents(platform=None, account=None, number=None, fbo_only=False, source_state=None):
    where, values = [], []
    for key, value in (('platform', platform), ('account', account), ('number', number)):
        if value is not None:
            where.append(key + '=?'); values.append(value)
    if fbo_only:
        where.append("scheme='fbo'")
    if source_state is not None:
        where.append("json_extract(payload,'$.source_state')=?"); values.append(source_state)
    with database() as con:
        rows = con.execute('SELECT payload,scheme FROM documents' + (' WHERE ' + ' AND '.join(where) if where else ''), values).fetchall()
    return [dict(json.loads(r['payload']), verified_scheme=r['scheme']) for r in rows]


def learn_scheme(ms_id, scheme):
    with database() as con:
        con.execute('UPDATE documents SET scheme=? WHERE id=?', (scheme, ms_id))


def retire(ms_id):
    with database() as con:
        con.execute('DELETE FROM documents WHERE id=?', (ms_id,))


def enqueue(proof, kind, *, force=False, baseline=False, reconcile=False):
    key = '|'.join(proof[k] for k in ('platform', 'account', 'number'))
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT * FROM observations WHERE key=?', (key,)).fetchone()
        if old and proof['fetched_at'] < old['stamp']:
            con.commit(); return False
        changed = not old or old['status'] != proof['status']
        body = json.dumps(proof, ensure_ascii=False)
        con.execute('INSERT INTO observations VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET stamp=excluded.stamp,status=excluded.status,payload=excluded.payload',
                    (key, proof['fetched_at'], proof['status'], body))
        if baseline or not (changed or force):
            con.commit(); return False
        if proof['status'] not in TARGETS:
            con.execute("""UPDATE jobs SET generation=generation+1,
              state=CASE WHEN state='running' THEN 'running' ELSE 'done' END,
              payload=?,result=? WHERE key=? AND state IN ('pending','running')""",
              (body, json.dumps({'reason': 'market_no_status_transition'}), key))
            con.commit(); return False
        con.execute('''INSERT INTO jobs(key,kind,available,payload,updated) VALUES(?,?,?,?,?)
          ON CONFLICT(key) DO UPDATE SET generation=jobs.generation+1,
          state=CASE WHEN jobs.state='running' OR (jobs.state='uncertain' AND NOT ?) THEN jobs.state ELSE 'pending' END,
          kind=CASE WHEN excluded.kind='urgent' OR (jobs.kind='urgent' AND jobs.state IN ('pending','running','uncertain')) THEN 'urgent' WHEN jobs.kind='fast' THEN 'fast' ELSE excluded.kind END,
          available=excluded.available,payload=excluded.payload,updated=excluded.updated''',
          (key, kind, time.time(), body, stamp(), bool(reconcile and kind=='urgent')))
        con.commit()
    return True


def claim(kind='fast'):
    import uuid
    now = time.time()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        if kind not in ('fast','night','urgent'):raise ValueError('status_job_kind_invalid')
        allowed = " AND kind IN ('fast','night')" if kind=='night' else " AND kind='"+kind+"'"
        row = con.execute("SELECT * FROM jobs WHERE ((state='pending' AND available<=?) OR (state='running' AND lease_until<?))" + allowed + ' ORDER BY available,key LIMIT 1', (now, now)).fetchone()
        if not row:
            con.commit(); return None
        token = str(uuid.uuid4())
        con.execute("UPDATE jobs SET state='running',lease=?,lease_until=? WHERE key=?", (token, now + 600, row['key']))
        con.commit()
    return {**dict(row), 'lease': token, 'proof': json.loads(row['payload'])}


def finish(job, result, *, retry_after=None, uncertain=False):
    state = 'uncertain' if uncertain else 'pending' if retry_after else 'done'
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        actual = con.execute('SELECT generation FROM jobs WHERE key=? AND lease=?', (job['key'], job['lease'])).fetchone()
        if actual and actual['generation'] != job['generation'] and not uncertain:
            state, retry_after = 'pending', 1
        con.execute('''UPDATE jobs SET state=?,available=?,lease=NULL,lease_until=NULL,result=?,updated=?
          WHERE key=? AND lease=?''', (state, time.time() + (retry_after or 0), json.dumps(result, ensure_ascii=False), stamp(), job['key'], job['lease']))
        con.commit()


def current(job):
    with database() as con:
        row = con.execute('SELECT generation,state,lease FROM jobs WHERE key=?', (job['key'],)).fetchone()
    return bool(row and row['generation'] == job['generation'] and row['state'] == 'running' and row['lease'] == job['lease'])


def reserve_slot():
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT value FROM settings WHERE key='next_write_slot'").fetchone()
        slot = max(time.time(), json.loads(row['value']) if row else 0)
        interval_row = con.execute("SELECT value FROM settings WHERE key='write_interval'").fetchone()
        interval = max(1.5, json.loads(interval_row['value']) if interval_row else 1.5)
        con.execute("INSERT INTO settings VALUES('next_write_slot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(slot + interval),))
        con.commit()
    return slot


def latency(seconds):
    current = setting('write_interval', 1.5)
    if seconds > 5:
        set_setting('write_interval', min(30, max(current * 2, seconds)))
    elif seconds < 2 and current > 1.5:
        set_setting('write_interval', max(1.5, current * 0.9))


def attempt_id(document, target):
    return hashlib.sha256((document['id'] + '|' + target + '|' + document['updated']).encode()).hexdigest()


def begin_attempt(document, target, proof, night):
    aid = attempt_id(document, target)
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute("SELECT * FROM attempts WHERE ms_id=? AND outcome IN ('intent','uncertain') ORDER BY at DESC LIMIT 1", (document['id'],)).fetchone()
        if old:
            result=json.loads(old['result'] or '{}')
            if (old['outcome']=='uncertain' and result.get('http')==429
                    and not result.get('transport_error') and old['target']==target
                    and old['before_updated']==document['updated']):
                con.execute("UPDATE attempts SET outcome='rejected' WHERE id=?",(old['id'],))
            else:
                con.commit(); return dict(old), False
        collision=con.execute('SELECT * FROM attempts WHERE id=?',(aid,)).fetchone()
        if collision:
            if collision['outcome']!='rejected':
                con.commit();return dict(collision),False
            import uuid
            aid=hashlib.sha256((aid+'|retry|'+uuid.uuid4().hex).encode()).hexdigest()
        count = con.execute('SELECT count(*) FROM attempts WHERE at>?', (time.time() - 3600,)).fetchone()[0]
        total = con.execute('SELECT count(*) FROM attempts WHERE night=?', (night,)).fetchone()[0] if night else 0
        if count >= 3000 or night and total >= 10000:
            con.commit(); raise RuntimeError('status_change_limit')
        con.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)', (aid, document['id'], target,
                    document['updated'], time.time(), night, 'intent', json.dumps({'before': document, 'proof': proof}, ensure_ascii=False), None))
        con.commit()
    return {'id': aid}, True


def end_attempt(aid, outcome, result):
    with database() as con:
        con.execute('UPDATE attempts SET outcome=?,result=? WHERE id=?', (outcome, json.dumps(result, ensure_ascii=False), aid))


def night_confirmed(night):
    with database() as con:
        return con.execute("SELECT count(*) FROM attempts WHERE night=? AND outcome='confirmed'", (night,)).fetchone()[0]

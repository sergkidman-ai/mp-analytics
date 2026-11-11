# поток: inv
"""Атомарная запись расчётного месяца и неизменяемый архив завершённых месяцев."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg2.extras import Json, execute_values
from core import db

ARCHIVE = Path('/opt/mp-analytics/.private/supplier_purchase_closed')
LOCK = 224231


def today():
    return dt.datetime.now(ZoneInfo('Europe/Moscow')).date()


def digest(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


def periods():
    return db.query('SELECT month,closed_at,refreshed_at,sha256 FROM supplier_purchase_period ORDER BY month')


def archive(month):
    row = db.query('SELECT * FROM supplier_purchase_period WHERE month=%s AND closed_at IS NOT NULL',
                   (month,))[0]
    if digest(row['payload']) != row['sha256']:
        raise ValueError('Supplier purchase archive checksum mismatch')
    ARCHIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
    ARCHIVE.chmod(0o700)
    target = ARCHIVE / f'{month:%Y-%m}_{row["sha256"]}.json'
    if target.exists():
        saved = json.loads(target.read_text())
        if saved['sha256'] != row['sha256'] or digest(saved['payload']) != row['sha256']:
            raise ValueError('Supplier purchase file checksum mismatch')
        return target
    temp = ARCHIVE / (target.name + f'.{os.getpid()}.tmp')
    try:
        with temp.open('x') as f:
            os.chmod(temp, 0o600)
            json.dump(row, f, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.link(temp, target)  # существующий архив не перезаписывается
    finally:
        temp.unlink(missing_ok=True)
    return target


def save(month, rows, documents):
    current = today().replace(day=1)
    if month > current:
        raise ValueError('Future supplier month')
    close = month < current
    payload = {'month': month.isoformat(), 'rows': rows, 'documents': documents,
               'source': 'MoySklad applicable supply/purchasereturn'}
    # jsonb должен содержать ту же каноническую форму, что и контрольная сумма.
    payload = json.loads(json.dumps(payload, default=str, ensure_ascii=False))
    checksum = digest(payload)
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
        cur.execute('SELECT closed_at FROM supplier_purchase_period WHERE month=%s FOR UPDATE', (month,))
        previous = cur.fetchone()
        if previous and previous[0] is not None:
            raise ValueError('Supplier purchase month is closed')
        cur.execute('DELETE FROM supplier_purchase_month WHERE month=%s', (month,))
        if rows:
            columns = ['month','grp','supply_sum','supply_docs','return_sum','return_docs','in_group']
            execute_values(cur, f'INSERT INTO supplier_purchase_month ({",".join(columns)}) VALUES %s',
                           [tuple(row[k] for k in columns) for row in rows])
        cur.execute('''INSERT INTO supplier_purchase_period(month,closed_at,sha256,payload)
            VALUES(%s,%s,%s,%s) ON CONFLICT(month) DO UPDATE SET closed_at=excluded.closed_at,
            refreshed_at=now(),sha256=excluded.sha256,payload=excluded.payload''',
            (month, dt.datetime.now(dt.timezone.utc) if close else None, checksum, Json(payload)))
    if close:
        archive(month)
    return len(rows)

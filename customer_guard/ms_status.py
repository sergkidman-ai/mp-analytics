# поток: inv
"""Publish already-read MS statuses into the page cache, without extra MS calls.

Reads from the existing status synchronizer supply full document identity. A
financial check remains dated independently; a status-only observation cannot
replace costs, prices, composition, jobs or marketplace observations.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

from psycopg2.extras import Json, RealDictCursor

from core import db
from customer_guard.domain import digest
from shipment_guard.config import identity


@contextmanager
def transaction():
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur


def apply(documents, observed_at, cur):
    if observed_at.tzinfo is None:
        raise ValueError('Status observation must have a timezone')
    if len(documents) > 20:
        raise ValueError('Status observation batch exceeds 20')
    prepared = {}
    for doc in documents:
        try:
            uid = str(UUID(str(doc.get('id'))))
        except (ValueError, TypeError, AttributeError):
            continue
        platform, account, *_ = identity(doc)
        name = doc.get('state', {}).get('name') if isinstance(doc.get('state'), dict) else None
        if not account or platform not in ('ozon','wb','yandex') or not isinstance(name, str) or not name:
            continue
        if uid in prepared:
            raise ValueError('Duplicate MS document in status page')
        prepared[uid] = (doc, platform, account, name)
    if not prepared:
        return {'observed':0,'changed':0}
    cur.execute("""SELECT id,platform,account,posting_number,ms_id::text,ms_status,
        ms_checked_at,ms_status_seen_at FROM customer_guard_orders
        WHERE ms_id=ANY(%s::uuid[]) FOR UPDATE""", (list(prepared),))
    observed, changed = 0, 0
    for row in cur.fetchall():
        doc, platform, account, name = prepared[row['ms_id']]
        if (row['platform'],row['account'],row['posting_number']) != (platform,account,doc.get('name')):
            continue
        times = [t for t in (row['ms_checked_at'],row['ms_status_seen_at']) if t is not None]
        if times and max(times) > observed_at:
            continue
        if row['ms_status'] != name:
            payload = {'previous_state':row['ms_status'], 'document':{
                'id':doc['id'],'name':doc['name'],'state':{'name':name},
                'updated':doc.get('updated'),'organization':doc.get('organization'),
                'agent':doc.get('agent')}, 'status_only':True}
            cur.execute("""INSERT INTO customer_guard_snapshots(order_id,source,api_version,fetched_at,hash,payload)
                VALUES(%s,'ms_status','remap/1.2',%s,%s,%s)""",
                (row['id'],observed_at,digest(payload),Json(payload)))
            changed += 1
        cur.execute('UPDATE customer_guard_orders SET ms_status=%s,ms_status_seen_at=%s WHERE id=%s',
                    (name,observed_at,row['id']))
        observed += 1
    return {'observed':observed,'changed':changed}


def observe(documents, observed_at=None):
    observed_at = observed_at or datetime.now(timezone.utc)
    with transaction() as cur:
        return apply(documents, observed_at, cur)

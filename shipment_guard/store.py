# поток: fin
"""All queue state transitions are transactions. Inbox keys survive payload cleanup."""
import hashlib
import time
import json
from contextlib import contextmanager
from uuid import uuid4
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from core import db
from shipment_guard import config

@contextmanager
def transaction(fast=False):
    started=time.monotonic()
    conn = psycopg2.connect(db.DATABASE_URL, connect_timeout=1 if fast else 5,
                          options='-c statement_timeout=600 -c lock_timeout=100' if fast else '-c statement_timeout=15000')
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                if fast:
                    remaining=max(50,int((1.25-(time.monotonic()-started))*1000))
                    c.execute('SET LOCAL statement_timeout = %s',(min(600,remaining),))
                yield c
    finally:
        conn.close()

ENQUEUE = """
INSERT INTO ms_shipment_jobs(account_id,demand_id,first_source,last_source,event_at,observed_version,next_attempt_at)
VALUES (%s,%s,%s,%s,%s,%s,now()+interval '15 seconds')
ON CONFLICT(account_id,demand_id) DO UPDATE SET
 requested_generation=ms_shipment_jobs.requested_generation+1,
 state=CASE WHEN ms_shipment_jobs.state='running' THEN 'running' ELSE 'pending' END,
 next_attempt_at=now()+interval '15 seconds', attempts=0, last_error=NULL,
 requested_at=now(),last_source=excluded.last_source,event_at=excluded.event_at,
 observed_version=coalesce(excluded.observed_version,ms_shipment_jobs.observed_version)
"""

def enqueue(c, account, demand, source, event_at=None, version=None):
    c.execute(ENQUEUE, (account,demand,source,source,event_at,version))


def accept(account, request, payload, events, event_at=None, transport='webhook', metadata=None):
    with transaction(fast=True) as c:
        c.execute("""INSERT INTO ms_webhook_inbox(account_id,request_id,payload,transport,delivery_metadata,event_at)
          VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING request_id""",
          (account,request,Json(payload),transport,Json(metadata or {}),event_at))
        if not c.fetchone():
            c.execute('UPDATE ms_webhook_inbox SET duplicate_count=duplicate_count+1,last_duplicate_at=now() WHERE account_id=%s AND request_id=%s',(account,request))
            return False
        # One generation per document per notification; every original event stays in payload.
        # Bulk statement keeps a large events array comfortably inside the ACK budget.
        c.execute("""INSERT INTO ms_shipment_jobs(account_id,demand_id,first_source,last_source,event_at,next_attempt_at)
          SELECT %s, value::uuid,%s,%s,%s,now()+interval '15 seconds'
          FROM jsonb_array_elements_text(%s)
          ON CONFLICT(account_id,demand_id) DO UPDATE SET
           requested_generation=ms_shipment_jobs.requested_generation+1,
           state=CASE WHEN ms_shipment_jobs.state='running' THEN 'running' ELSE 'pending' END,
           next_attempt_at=now()+interval '15 seconds',attempts=0,last_error=NULL,
           requested_at=now(),last_source=excluded.last_source,event_at=excluded.event_at""",
          (account,transport,transport,event_at,Json(sorted({e['demand_id'] for e in events}))))
    return True


def claim():
    since=config.pilot_since()
    scope="AND last_source='webhook' AND requested_at >= %s" if since else ''
    params=(since,str(uuid4())) if since else (str(uuid4()),)
    with transaction() as c:
        c.execute(f"""WITH candidate AS (
          SELECT account_id,demand_id FROM ms_shipment_jobs
          WHERE ((state IN ('pending','retry') AND next_attempt_at<=now())
             OR (state='running' AND locked_until<now()))
          {scope}
          ORDER BY next_attempt_at,first_seen_at FOR UPDATE SKIP LOCKED LIMIT 1)
          UPDATE ms_shipment_jobs j SET state='running',lease_token=%s,
            locked_until=now()+interval '10 minutes',attempts=attempts+1
          FROM candidate x WHERE j.account_id=x.account_id AND j.demand_id=x.demand_id RETURNING j.*""",params)
        r=c.fetchone()
        return dict(r) if r else None


def heartbeat(job):
    with transaction() as c:
        c.execute("UPDATE ms_shipment_jobs SET locked_until=now()+interval '10 minutes' WHERE account_id=%s AND demand_id=%s AND lease_token=%s AND state='running'",(job['account_id'],job['demand_id'],job['lease_token']))
        if c.rowcount != 1:
            raise RuntimeError('lease_lost')


def skip_outside_pilot(job):
    """Release routing-only work without touching financial assessments or FIFO dates."""
    with transaction() as c:
        c.execute("""UPDATE ms_shipment_jobs SET processed_generation=%s,
          state=CASE WHEN requested_generation=%s THEN 'done' ELSE 'pending' END,
          locked_until=NULL,lease_token=NULL,processed_at=now(),last_checked_at=now(),last_error=NULL
          WHERE account_id=%s AND demand_id=%s AND lease_token=%s AND state='running'
          RETURNING state""",(job['requested_generation'],job['requested_generation'],
          job['account_id'],job['demand_id'],job['lease_token']))
        row=c.fetchone()
        return row is not None and row['state']=='done'


def finish(job, result, raw_document=None, raw_fifo=None, raw_order=None, retry=False):
    fingerprint=hashlib.sha256(json.dumps(result,sort_keys=True,default=str).encode()).hexdigest()
    with transaction() as c:
        c.execute('SELECT * FROM ms_shipment_jobs WHERE account_id=%s AND demand_id=%s FOR UPDATE',(job['account_id'],job['demand_id']))
        live=c.fetchone()
        if not live or live['lease_token'] != job['lease_token'] or live['state'] != 'running':
            return False
        current=live['requested_generation']==job['requested_generation']
        if current:
            c.execute('UPDATE ms_shipment_assessments SET is_current=false WHERE account_id=%s AND demand_id=%s AND is_current',(job['account_id'],job['demand_id']))
        c.execute("""INSERT INTO ms_shipment_assessments(account_id,demand_id,generation,fingerprint,document_version,
          document_name,document_moment,platform,account,organization,order_id,status,signals,result,
          raw_document,raw_fifo,raw_order,discovery_source,event_at,received_at,is_current)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(account_id,demand_id,fingerprint) DO UPDATE SET is_current=excluded.is_current OR ms_shipment_assessments.is_current""",
          (job['account_id'],job['demand_id'],job['requested_generation'],fingerprint,result.get('updated'),
           result.get('name'),result.get('moment'),result.get('platform'),result.get('account'),result.get('organization'),
           result.get('order_id'),result['status'],Json(result.get('signals',[])),Json(result),
           Json(raw_document),Json(raw_fifo),Json(raw_order),job['last_source'],job['event_at'],job['requested_at'],current))
        state='pending' if not current else 'retry' if retry and job['attempts'] < 3 else 'error' if result['status']=='data_error' else 'done'
        c.execute("""UPDATE ms_shipment_jobs SET processed_generation=%s,state=%s,
          locked_until=NULL,lease_token=NULL,processed_at=now(),last_checked_at=now(),
          fifo_checked_at=CASE WHEN %s THEN now() ELSE fifo_checked_at END,
          next_attempt_at=CASE WHEN %s THEN now()+interval '60 seconds' ELSE next_attempt_at END,
          last_error=%s, observed_version=CASE WHEN %s THEN coalesce(%s,observed_version) ELSE observed_version END
          WHERE account_id=%s AND demand_id=%s""",
          (job['requested_generation'],state,raw_fifo is not None,state=='retry',
           '; '.join(result.get('errors',[])) or None,current,result.get('updated'),job['account_id'],job['demand_id']))
    return current


def metric(name, value=1, details=None, replace=False):
    with transaction() as c:
        c.execute("""INSERT INTO ms_shipment_metrics(name,value,details) VALUES(%s,%s,%s)
          ON CONFLICT(name) DO UPDATE SET value=CASE WHEN %s THEN excluded.value ELSE ms_shipment_metrics.value+excluded.value END,
          details=excluded.details,updated_at=now()""",(name,value,Json(details or {}),replace))


def cleanup():
    # Never remove dedup keys, jobs, assessments, or error/pending payloads.
    with transaction() as c:
        c.execute("""UPDATE ms_webhook_inbox i SET payload=NULL WHERE payload IS NOT NULL
          AND received_at<now()-interval '90 days'
          AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(i.payload->'events') e
            JOIN ms_shipment_jobs j ON j.account_id=i.account_id
            AND j.demand_id::text=split_part(e->'meta'->>'href','/entity/demand/',2)
            WHERE j.state<>'done')""")

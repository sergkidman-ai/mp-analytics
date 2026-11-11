# поток: inv
"""Durable missing-MS events; guard commits its check before publishing a request."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
from core import db

EVENT_ROOT = Path('/opt/mp-analytics/runtime/customer-guard-ms-recovery-events')


@contextmanager
def queue_lock():
    EVENT_ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(EVENT_ROOT/'queue.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:os.close(fd)


def event_files():
    return sorted((EVENT_ROOT/'inbox').glob('*.json'))


def publish(row):
    # Only a normalized order identity and check ID are persisted, never API credentials.
    data={k:row[k] for k in ('order_id','check_id','platform','account','number')}
    if type(data['order_id']) is not int or type(data['check_id']) is not int or min(data['order_id'],data['check_id'])<=0:
        raise ValueError('invalid_recovery_event_identity')
    with queue_lock():
        inbox=EVENT_ROOT/'inbox';inbox.mkdir(exist_ok=True,mode=0o700)
        (EVENT_ROOT/'pending').touch(mode=0o600,exist_ok=True)
        target=inbox/(str(data['order_id'])+'-'+str(data['check_id'])+'.json')
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=inbox,prefix='.',suffix='.tmp',delete=False) as handle:
            temp=Path(handle.name)
            json.dump(data,handle,ensure_ascii=False);handle.flush();os.fsync(handle.fileno())
        os.replace(temp,target)
        fd=os.open(inbox,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        fd=os.open(EVENT_ROOT,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return target


def enqueue_missing(order_id):
    rows=db.query("""SELECT o.id order_id,c.id check_id,o.platform,o.account,o.posting_number number
      FROM customer_guard_orders o JOIN customer_guard_checks c ON c.id=o.latest_check_id
      WHERE o.id=%s AND o.ms_id IS NULL AND o.mp_status='awaiting_packaging'
      AND o.ms_due_at<=now() AND c.generation=o.generation
      AND c.result->>'signal'='missing_ms'""",(order_id,))
    if not rows:return False
    publish(rows[0]);return True


def sync_pending(ledger):
    # Serialize with publishers so an arriving event can never lose its wake-up marker.
    with queue_lock():
        pending=any(r.get('pending_event') or r.get('needs_processing') for r in ledger.values())
        pending = bool(pending or event_files())
        if pending:
            (EVENT_ROOT/'pending').touch(mode=0o600,exist_ok=True)
        else:
            (EVENT_ROOT/'pending').unlink(missing_ok=True)
        return pending

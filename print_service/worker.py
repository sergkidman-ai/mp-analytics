# поток: print
"""One owned worker; independent sync, requested pairs and label-only warming.

Run only after explicit schema installation/deployment. It never creates tables.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from datetime import datetime, timezone, timedelta
import threading
import time

from print_service.clients import PrintClients
from print_service.domain import MOSCOW, PrintBlocked
from print_service.service import PrintService

LOCK = 0x5052494E54


def sync_due(state, now):
    last = state['started_at']
    return (not last or state['day'] != now.astimezone(MOSCOW).date()
            or now - last >= timedelta(minutes=30))


def run(service=None):
    service = service or PrintService()
    queue = service.queue
    if not queue.installed(): raise PrintBlocked('print_schema_not_installed')
    with queue.connect() as lease:
        lease.autocommit = True
        with lease.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', (LOCK,))
            if not cursor.fetchone()[0]: raise PrintBlocked('print_worker_already_running')
        mutex = threading.Lock()
        def check():
            try:
                with mutex, lease.cursor() as cursor: cursor.execute('SELECT 1')
            except Exception: raise PrintBlocked('print_worker_lease_lost') from None
        service.clients.lease_check = check
        with queue.transaction() as cursor:
            cursor.execute("UPDATE batches SET generation=generation+1 WHERE state='building'")
            cursor.execute("UPDATE sync_runs SET state='pending' WHERE state='running'")
            cursor.execute("UPDATE label_requests SET state='pending' WHERE state='running'")
        sync_future = batch_future = label_future = None
        label_snapshot, label_rows = None, deque()
        last_heartbeat = last_scheduled = 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            while True:
                check()
                now = service.clock()
                if time.monotonic() - last_heartbeat >= 20:
                    queue.heartbeat(now); last_heartbeat = time.monotonic()
                status = queue.sync_clock()
                due = sync_due(status, now)
                # Failed syncs retry at most once per minute; successful syncs
                # reset the actual 30-minute deadline in current_snapshot.
                if due and time.monotonic() - last_scheduled >= 60:
                    queue.enqueue_sync(); last_scheduled = time.monotonic()
                if sync_future is None or sync_future.done():
                    if sync_future: sync_future.result()
                    sid = queue.start_sync()
                    sync_future = pool.submit(service.run_sync, sid) if sid else None
                if batch_future is None or batch_future.done():
                    if batch_future: batch_future.result()
                    batch = queue.pending_batch()
                    batch_future = pool.submit(service.run_batch, batch) if batch else None
                if label_future is None or label_future.done():
                    if label_future: label_future.result()
                    request = queue.start_label_request(now=now)
                    if request:
                        label_future = pool.submit(service.run_label_request, request)
                    else:
                        snapshot = (status['started_at'], status['day'])
                        if snapshot != label_snapshot:
                            label_rows = deque(service.labels.candidates(now))
                            label_snapshot = snapshot
                        label_future = pool.submit(service.run_label_prefetch, label_rows.popleft()) if label_rows else None
                time.sleep(2)


if __name__ == '__main__':
    try: run()
    except PrintBlocked as error: raise SystemExit(str(error))

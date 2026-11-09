# поток: print (uses the existing inv status writer)
"""Exact confirmed cancellations from printing; no independent MS writer."""
from datetime import datetime, timezone
import fcntl
import os
import re
import signal
import threading
import time
from uuid import UUID

from ops import ms_market_status_state as state


def request_cancel(order_id, account, number, posting):
    order_id=str(UUID(str(order_id)))
    if (account not in ('oz_acc1','oz_acc2') or not re.fullmatch(r'\d+-\d+-\d+',number)
            or posting.get('posting_number')!=number or posting.get('status')!='cancelled'):
        raise ValueError('urgent_cancellation_identity_invalid')
    proof=dict(platform='ozon',account=account,number=number,ms_id=order_id,
               scheme='fbs',status='cancelled',detail='cancelled',
               fetched_at=datetime.now(timezone.utc).isoformat(),evidence='print_ozon_cancelled')
    state.enqueue(proof,'urgent',force=True,reconcile=True)
    return {'queued':True,'number':number}


def step(on_updated=None):
    from ops.ms_market_status_live import apply_job
    job=state.claim('urgent')
    if not job:return None
    try:
        if job['proof'].get('status')!='cancelled' or job['proof'].get('platform')!='ozon':
            result={'result':'urgent_scope_rejected'}
        else:result=apply_job(job)
        state.finish(job,result,retry_after=result.get('retry_after'),uncertain=result.get('result')=='uncertain')
        if on_updated and result.get('result') in ('updated','already_updated'):
            try:on_updated()
            except Exception:result['print_sync_failed']=True
    except Exception as exc:
        result={'result':'error','reason':type(exc).__name__}
        state.finish(job,result,retry_after=30)
    return result


def flush_print_sync(queue):
    if not state.setting('urgent_print_sync_needed',False):return False
    with queue.transaction() as cursor:
        cursor.execute("SELECT 1 FROM sync_runs WHERE state='running'")
        if cursor.fetchone():return False
    queue.enqueue_sync()
    state.set_setting('urgent_print_sync_needed',False)
    return True


def run():
    state.RUNTIME.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(state.RUNTIME/'urgent.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from print_service.queue import PrintQueue
        queue=PrintQueue()
        stop=threading.Event()
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *args:stop.set())
        while not stop.is_set():
            step(on_updated=lambda:state.set_setting('urgent_print_sync_needed',True))
            flush_print_sync(queue)
            stop.wait(2)
    finally:os.close(fd)


if __name__=='__main__':run()

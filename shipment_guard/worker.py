# поток: fin
import argparse
import logging
import signal
import time
from datetime import datetime, timezone
import psycopg2
from core import db
from shipment_guard import config, store, sync
from shipment_guard.ms import MSClient, NotFound
from shipment_guard.adapters import OrderAdapter
from shipment_guard.economics import assess
LOG=logging.getLogger('shipment_guard')
LOCK_ID=7348201701

class Worker:
    def __init__(self,client=None,adapter=None):
        self.client=client or MSClient(); self.adapter=adapter or OrderAdapter()
        self.stop=False

    def process(self,job):
        started=time.monotonic(); initial_requests=self.client.requests
        document=raw_fifo=positions=order=None
        def pulse():
            if getattr(self,'lock_connection',None) is not None:
                with self.lock_connection.cursor() as c: c.execute('SELECT 1')
            store.heartbeat(job)
        self.client.pulse=self.adapter.pulse=pulse
        try:
            did=str(job['demand_id'])
            try: document=self.client.get('/entity/demand/'+did)
            except NotFound:
                previous=db.query('SELECT result FROM ms_shipment_assessments WHERE account_id=%s AND demand_id=%s AND is_current',(job['account_id'],did))
                result=dict(previous[0]['result']) if previous else {'name':did}
                result.update(status='deleted',signals=[],errors=[],deleted=True)
                store.finish(job,result)
                return
            if document.get('accountId')!=str(job['account_id']): raise RuntimeError('document_account_mismatch')
            positions=list(self.client.rows('/entity/demand/'+did+'/positions',{'expand':'assortment'}))
            raw_fifo=list(self.client.rows('/report/stock/byoperation',{'operation.id':did}))
            confirmed,order=self.adapter.confirm(document,positions)
            _,account,_,org_id=config.identity(document)
            rules=db.query('SELECT * FROM ms_shipment_rules WHERE account=%s AND organization_id=%s AND enabled',(account,org_id)) if account else []
            result=assess(document,positions,raw_fifo,confirmed,rules[0] if rules else None)
            # Store positions with raw demand; replay economics without another API read.
            document['guard_positions']=positions
            transient=result['status']=='data_error' and any(
                ('FIFO отсутствует' in error or 'network_error' in error or '_429' in error or 'not_found' in error)
                for error in result.get('errors',[]))
            store.finish(job,result,document,raw_fifo,order,retry=transient)
        except Exception as e:
            # Never log requests exceptions/URLs/response bodies (may contain credentials/PII).
            code=type(e).__name__
            if hasattr(e,'retry_after'): code=str(e)
            result={'name':document.get('name') if document else str(job['demand_id']),
              'updated':document.get('updated') if document else None,
              'status':'data_error','signals':['data_error'],'errors':['Ошибка обязательного источника: '+code]}
            if document:
                platform,account,org,_=config.identity(document)
                result.update(platform=platform,account=account,organization=org,moment=sync.config_time(document.get('moment')).isoformat())
            store.finish(job,result,document,raw_fifo,order,retry=True)
            store.metric('worker_errors',details={'code':code})
        finally:
            self.client.pulse=self.adapter.pulse=lambda:None
            store.metric('processed_jobs',details={'ms_reads':self.client.requests-initial_requests,
                 'processing_seconds':round(time.monotonic()-started,3),
                 'since_received_seconds':round((datetime.now(timezone.utc)-job['requested_at']).total_seconds(),3)})

    def run(self,once=False):
        # Session lock excludes a second worker and manual drains across processes.
        lock=psycopg2.connect(db.DATABASE_URL,connect_timeout=5)
        lock.autocommit=True
        self.lock_connection=lock
        try:
            with lock.cursor() as c:
                c.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,))
                if not c.fetchone()[0]: raise RuntimeError('another shipment worker owns the MS limiter')
            account=config.account_id(); window=None; jobs_since_page=0
            next_recent=next_check=next_cleanup=0
            while not self.stop:
                # A lost advisory-lock connection must stop API traffic immediately.
                with lock.cursor() as c: c.execute('SELECT 1')
                store.metric('worker_heartbeat',replace=True)
                job=store.claim()
                if job:
                    self.process(job); jobs_since_page+=1
                if once: return
                now=time.monotonic()
                if window is None:
                    state=db.query('SELECT last_attempt_at FROM ms_shipment_sync_state WHERE account_id=%s',(account,))
                    if state and (not state[0]['last_attempt_at'] or (datetime.now(timezone.utc)-state[0]['last_attempt_at']).total_seconds()>=1800):
                        window=sync.reconcile(self.client,account)
                if window is not None and (not job or jobs_since_page>=10):
                    try: next(window)
                    except StopIteration: window=None
                    except Exception:
                        window=None; store.metric('sync_errors'); LOG.warning('Reconciliation failed; checkpoint preserved')
                    jobs_since_page=0
                if now>=next_recent:
                    sync.recheck_recent(account); next_recent=now+1800
                if now>=next_check:
                    try: sync.check_tracked(self.client,account)
                    except Exception: store.metric('tracked_check_errors')
                    next_check=now+30
                if now>=next_cleanup:
                    store.cleanup(); next_cleanup=now+86400
                if not job: time.sleep(1)
        finally:
            lock.close()


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--once',action='store_true'); args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    worker=Worker()
    def stop(*_): worker.stop=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    worker.run(args.once)

if __name__=='__main__': main()

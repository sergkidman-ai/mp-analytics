# поток: fin
"""Fixed upper bound, Moscow timestamps, overlap, full pagination, commit before checkpoint."""
from datetime import datetime, timedelta, timezone
from shipment_guard import store, config

def ms_stamp(dt): return dt.astimezone(config.MS_TZ).strftime('%Y-%m-%d %H:%M:%S')

def reconcile(client, account, now=None):
    now=now or datetime.now(timezone.utc)
    with store.transaction() as c:
        c.execute('SELECT checkpoint FROM ms_shipment_sync_state WHERE account_id=%s',(account,))
        row=c.fetchone()
        if not row: raise RuntimeError('sync checkpoint must be initialized explicitly')
        lower=row['checkpoint']-timedelta(minutes=5)
        upper=now-timedelta(seconds=30)
        c.execute('UPDATE ms_shipment_sync_state SET last_attempt_at=now(),window_upper=%s,last_error=NULL WHERE account_id=%s',(upper,account))
    offset=0
    previous=None
    try:
        # Offset pagination can shift when documents change during the scan. Require
        # two identical consecutive bounded passes; otherwise preserve checkpoint.
        stable=False
        for scan in range(3):
            offset=0; seen=[]
            while True:
                page=client.get('/entity/demand',{'filter':f'updated>={ms_stamp(lower)};updated<={ms_stamp(upper)}',
                   'order':'updated,asc;id,asc','limit':20,'offset':offset})
                rows=page.get('rows')
                if not isinstance(rows,list): raise RuntimeError('sync_missing_rows')
                with store.transaction() as c:
                    for d in rows:
                        if d.get('accountId')!=account: raise RuntimeError('sync_account_mismatch')
                        seen.append((d['id'],d.get('updated')))
                        c.execute('SELECT observed_version FROM ms_shipment_jobs WHERE account_id=%s AND demand_id=%s FOR UPDATE',(account,d['id']))
                        old=c.fetchone()
                        if not old or old['observed_version']!=d.get('updated'):
                            store.enqueue(c,account,d['id'],'reconciliation',config_time(d.get('updated')),d.get('updated'))
                offset+=len(rows)
                yield len(rows)
                size=page.get('meta',{}).get('size')
                if size is None: raise RuntimeError('sync_missing_size')
                if offset>=size: break
                if not rows: raise RuntimeError('sync_incomplete_page')
            if previous==seen:
                stable=True; break
            previous=seen
        if not stable: raise RuntimeError('sync_unstable_pagination')
        with store.transaction() as c:
            c.execute('UPDATE ms_shipment_sync_state SET checkpoint=%s,last_success_at=now(),last_error=NULL WHERE account_id=%s',(upper,account))
        store.metric('sync_documents',offset)
    except Exception:
        with store.transaction() as c:
            c.execute("UPDATE ms_shipment_sync_state SET last_error='sync_failed_checkpoint_preserved' WHERE account_id=%s",(account,))
        raise

def config_time(value):
    return datetime.fromisoformat(value).replace(tzinfo=config.MS_TZ) if value else None


def recheck_recent(account):
    # At most 20 documents per half-hour, rotating by FIFO check time. Same durable queue.
    with store.transaction() as c:
        c.execute("""SELECT j.demand_id FROM ms_shipment_jobs j
          JOIN ms_shipment_assessments a ON a.account_id=j.account_id AND a.demand_id=j.demand_id AND a.is_current
          WHERE j.account_id=%s AND j.state IN ('done','error') AND a.status<>'deleted'
            AND a.document_moment>=now()-interval '7 days'
            AND coalesce(j.fifo_checked_at,j.first_seen_at)<now()-interval '4 hours'
          ORDER BY j.fifo_checked_at NULLS FIRST,j.first_seen_at LIMIT 20 FOR UPDATE OF j SKIP LOCKED""",(account,))
        for row in c.fetchall(): store.enqueue(c,account,row['demand_id'],'fifo_recheck')


def check_tracked(client,account):
    # Rotate through ALL tracked live IDs, including old documents; not just current signals.
    with store.transaction() as c:
        c.execute("""SELECT j.demand_id,j.observed_version FROM ms_shipment_jobs j
          JOIN ms_shipment_assessments a ON a.account_id=j.account_id AND a.demand_id=j.demand_id AND a.is_current
          WHERE j.account_id=%s AND j.state IN ('done','error') AND a.status<>'deleted'
            AND coalesce(j.last_checked_at,j.first_seen_at)<now()-interval '24 hours'
          ORDER BY j.last_checked_at NULLS FIRST LIMIT 1""",(account,))
        row=c.fetchone()
    if not row: return
    from shipment_guard.ms import NotFound
    try:
        doc=client.get('/entity/demand/'+str(row['demand_id']))
        changed=doc.get('updated')!=row['observed_version']
    except NotFound: changed=True
    with store.transaction() as c:
        c.execute('UPDATE ms_shipment_jobs SET last_checked_at=now() WHERE account_id=%s AND demand_id=%s',(account,row['demand_id']))
        if changed: store.enqueue(c,account,row['demand_id'],'tracked_id_check')

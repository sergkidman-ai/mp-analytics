# поток: inv
"""Repair confirmed missing MS orders using the existing importer and processors."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo
from dateutil.parser import isoparse

from core import db, ms_api
from ops.order_priority import order_key
from customer_guard.wb import WBClient
from customer_guard import recovery_events as events
from ops import fbs_ms_import_scheduler as scheduler
from ops import fbs_ms_import_next5 as importer
from ops import fbs_ms_post_import_status as status_step
from ops import fbs_ms_post_import_shipments as shipment_step
from ops.fbs_ms_import_read import find_customerorder

RUNTIME = scheduler.ROOT / '.private/customer_guard_ms_recovery'
MAX_CREATE = 3
MAX_SCAN = 20
ACCOUNTS = ('oz_acc1', 'oz_acc2', 'wb_acc1', 'wb_acc2', 'ya_acc1')


def candidates(order_ids):
    if not order_ids:return []
    # Only a successful, current MS check can establish absence. No API scan here.
    return db.query("""SELECT o.id,o.platform,o.account,o.posting_number,o.mp_payload,
      o.first_seen_at,o.ms_due_at FROM customer_guard_orders o
      JOIN customer_guard_checks c ON c.id=o.latest_check_id
      WHERE o.ms_id IS NULL AND o.mp_status='awaiting_packaging'
      AND o.ms_due_at<=now() AND c.generation=o.generation
      AND c.result->>'signal'='missing_ms' AND o.account=ANY(%s) AND o.id=ANY(%s)
      ORDER BY o.first_seen_at,o.id""", (list(ACCOUNTS),list(order_ids)))


def key(row):
    return '|'.join((row['platform'],row['account'],row['posting_number']))


def safe_error(exc):
    result = {'error':type(exc).__name__}
    if isinstance(exc, (RuntimeError,ValueError)):
        result['reason'] = str(exc)[:160]
    response = getattr(exc,'response',None)
    if response is not None:
        result['http'] = response.status_code
    return result


def own_proof(number, ms_id):
    folder = importer.OUT / ('next5_'+number)
    before, after = folder/'preflight.json', folder/'readback.json'
    if not (before.exists() and after.exists()):
        return None
    verified = json.loads(after.read_text())
    if verified.get('name')!=number or verified.get('id')!=ms_id:
        return None
    return json.loads(before.read_text())


def fresh_source(row, new_wb, stats, stats_days):
    platform, account, number = row['platform'],row['account'],row['posting_number']
    source = {'platform':platform,'account':account,'number':number}
    if platform=='wb':
        if account not in new_wb:
            raw = WBClient(account=account).call('/api/v3/orders/new')['orders']
            new_wb[account] = {str(p['id']):p for p in raw}
        raw = new_wb[account].get(number)
        if raw is None or raw.get('deliveryType')!='fbs':
            raise RuntimeError('wb_not_new_fbs')
        source.update(created_at=raw['createdAt'],rid=raw['rid'],article=raw['article'],
            buyer_price_kopeks=raw['price'],currency_code=raw['currencyCode'],
            converted_price_kopeks=raw.get('convertedPrice'),
            converted_currency_code=raw.get('convertedCurrencyCode'))
        created = isoparse(raw['createdAt'])
        if created.tzinfo is None:raise RuntimeError('wb_creation_timezone_missing')
        day = created.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
        if account in stats_days and stats_days[account]!=day:
            raise RuntimeError('wb_statistics_day_budget')
        if account in stats_days and stats.get(account[-4:]) is None:
            raise RuntimeError('wb_statistics_temporarily_unavailable')
        if account not in stats_days:
            stats_days[account] = day
            stats[account[-4:]] = None
            old = os.environ.get('FBS_SCAN_START_UTC')
            os.environ['FBS_SCAN_START_UTC'] = raw['createdAt']
            try:
                stats[account[-4:]] = importer.wb_statistics(account[-4:])
                stats_days[account] = day
            finally:
                if old is None:os.environ.pop('FBS_SCAN_START_UTC',None)
                else:os.environ['FBS_SCAN_START_UTC'] = old
    elif platform=='yandex':
        source['campaign_id'] = row['mp_payload']['market_order']['campaignId']
    elif platform!='ozon':
        raise RuntimeError('unsupported_platform')
    # source() point-reads MP identity/status/prices, rejecting cancelled/packed orders.
    fresh = importer.source(source,stats)
    source.update(created_at=fresh['created_at'],status=fresh['status'],is_express=fresh.get('is_express',False),
        products=[{'code':p['market_code'],'quantity':p['quantity']} for p in fresh['lines']])
    return source


def process(numbers):
    """Scope the existing status/demand/marketplace stages to proven import artifacts."""
    scope = set(numbers)
    proofs = {number:proof for number,proof in numbers.items() if proof is not None}
    original_proofs, original_pending = status_step.imported_proofs, shipment_step.pending
    try:
        status_step.imported_proofs = lambda: proofs
        shipment_step.pending = lambda:[p for p in original_pending() if p['name'] in scope]
        statuses = status_step.run(True)
        shipments = shipment_step.run(True)
        return {'status':statuses,'shipments':shipments}
    finally:
        status_step.imported_proofs, shipment_step.pending = original_proofs,original_pending


def ingest_events(ledger, ledger_path, apply, result):
    with events.queue_lock():
        for path in events.event_files()[:MAX_SCAN]:
            try:
                event=json.loads(path.read_text())
                if (type(event.get('order_id')) is not int or type(event.get('check_id')) is not int
                        or min(event['order_id'],event['check_id'])<=0
                        or event.get('account') not in ACCOUNTS
                        or not isinstance(event.get('number'),str)
                        or event.get('platform') not in ('ozon','wb','yandex')):
                    raise ValueError('invalid_recovery_event')
                k='|'.join(event[n] for n in ('platform','account','number'))
                record=ledger.get(k,{})
                if event['check_id']>record.get('last_event_check',0):
                    record.update(guard_order_id=event['order_id'],number=event['number'],
                                  pending_event=True,next_at=time.time(),last_event_check=event['check_id'])
                    ledger[k]=record
                if apply:
                    # Commit receipt before removing the wake-up file.
                    scheduler.atomic_save(ledger_path,ledger)
                    path.unlink()
            except (ValueError,KeyError,TypeError) as exc:
                result['errors'].append(dict(stage='event',**safe_error(exc)))
                if apply:
                    archive=RUNTIME/'invalid_events';archive.mkdir(exist_ok=True,mode=0o700)
                    os.replace(path,archive/path.name)


def run(apply=False):
    RUNTIME.mkdir(parents=True,exist_ok=True,mode=0o700)
    ledger_path=RUNTIME/'ledger.json'
    own_fd=os.open(RUNTIME/'worker.lock',os.O_CREAT|os.O_RDWR,0o600)
    result={'at':datetime.now().astimezone().isoformat(),'apply':apply,
            'candidates':0,'created':[],'existing':[],'errors':[]}
    ledger={};fd=None;owns=False;loaded=False
    previous_limits=ms_api.TIMEOUT,ms_api.RETRIES
    try:
        try:fcntl.flock(own_fd,fcntl.LOCK_EX|fcntl.LOCK_NB);owns=True
        except BlockingIOError:
            return dict(result,busy=True,retry_pending=True)
        ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        loaded=True
        ingest_events(ledger,ledger_path,apply,result)
        ready_ids=[r['guard_order_id'] for r in ledger.values()
                   if r.get('pending_event') and r.get('next_at',0)<=time.time()][:MAX_SCAN]
        # No event means no DB query or external API calls.
        rows=candidates(ready_ids) if ready_ids else []
        result['candidates']=len(rows)
        if not apply:
            result['orders']=[{'number':r['posting_number'],'account':r['account']} for r in rows]
            scheduler.atomic_save(RUNTIME/'preview.json',result)
            return result
        found_ids={r['id'] for r in rows}
        for record in ledger.values():
            if record.get('pending_event') and record.get('guard_order_id') in ready_ids and record['guard_order_id'] not in found_ids:
                record.update(pending_event=False,outcome='no_longer_confirmed_missing')
        scheduler.atomic_save(ledger_path,ledger)
        fd=os.open(scheduler.LOCK,os.O_CREAT|os.O_RDWR,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            result['busy']=True
            return result
        ms_api.TIMEOUT,ms_api.RETRIES=45,2
        new_wb,stats,stats_days = {},{},{}
        promised = defaultdict(Decimal)
        def failure_record(record,exc):
            failure=safe_error(exc);result['errors'].append(dict(number=record['number'],**failure))
            attempts=record.get('failures',0)+1
            manual=str(exc) in ('prior_intent_requires_reconciliation','post_uncertain_no_retry',
                                'ms_readback_mismatch_stop_batch')
            record.update(outcome='manual_reconciliation' if manual else 'retry',pending_event=not manual,
                          failures=attempts,next_at=time.time()+min(3600,900*attempts),last_error=failure)

        # Resolve fresh delivery types before the creation cap; otherwise an
        # express event behind three ordinary events could lose its priority.
        prepared=[]
        for row in rows:
            number=row['posting_number'];k=key(row)
            record=dict(ledger.get(k,{}),number=number)
            try:
                existing=find_customerorder(number)
                if existing:
                    proof=own_proof(number,existing['id'])
                    record.update(outcome='exists',pending_event=False,ms_id=existing['id'],
                                  next_at=time.time()+21600,
                                  needs_processing=proof is not None and not record.get('processed',False))
                    result['existing'].append(number)
                    if proof is None and (importer.OUT/('next5_'+number)/'intent.json').exists():
                        record['outcome']='manual_reconciliation'
                        result['errors'].append({'number':number,'reason':'existing_import_requires_reconciliation'})
                else:
                    source=fresh_source(row,new_wb,stats,stats_days)
                    prepared.append((row,source))
            except Exception as exc:
                failure_record(record,exc)
            ledger[k]=record
            scheduler.atomic_save(ledger_path,ledger)
        for row,source in sorted(prepared,key=lambda pair:order_key(pair[1]))[:MAX_CREATE]:
            number=row['posting_number'];k=key(row);record=ledger[k]
            try:
                prices,digest=scheduler.prices_for([source])
                created=importer.create_one(source,stats,prices,digest,promised)
                known=scheduler.known_ms_numbers();known.add(number)
                scheduler.atomic_save(scheduler.KNOWN,{'numbers':sorted(known)})
                after=json.loads((importer.OUT/('next5_'+number)/'readback.json').read_text())
                record.update(outcome='created',pending_event=False,ms_id=after['id'],next_at=time.time()+21600,
                              needs_processing=True,processed=False)
                result['created'].append(created)
            except Exception as exc:
                failure_record(record,exc)
            scheduler.atomic_save(ledger_path,ledger)
            scheduler.atomic_save(RUNTIME/'latest.json',result)
        # Also resume a successful import interrupted before its processor ran.
        pending={}
        for record in ledger.values():
            if record.get('needs_processing') and record.get('processing_next_at',0)<=time.time():
                proof=own_proof(record['number'],record['ms_id'])
                if proof is not None:pending[record['number']]=proof
        pending=dict(sorted(pending.items(),key=lambda item:order_key(item[1]))[:MAX_CREATE])
        if pending:
            try:
                processing=process(pending);result['processing']=processing
                statuses={r['number']:r for r in processing['status']['results']}
                shipment_errors={r['number'] for r in processing['shipments'].get('errors',[])}
                for record in ledger.values():
                    if record.get('number') not in pending:continue
                    state=statuses.get(record['number'],{})
                    failed=state.get('result') in ('error','deferred_limit') or record['number'] in shipment_errors
                    record.update(needs_processing=failed,processed=not failed,
                                  processing_next_at=time.time()+900,processing_result=state)
                result['errors'].extend(processing['status'].get('errors',[]))
                result['errors'].extend(processing['shipments'].get('errors',[]))
            except Exception as exc:
                failure=safe_error(exc);result['errors'].append(dict(stage='processing',**failure))
                for record in ledger.values():
                    if record.get('number') in pending:record['processing_next_at']=time.time()+900
        scheduler.atomic_save(ledger_path,ledger)
        scheduler.atomic_save(RUNTIME/'latest.json',result)
        return result
    finally:
        ms_api.TIMEOUT,ms_api.RETRIES=previous_limits
        try:
            if apply and owns and loaded:
                scheduler.atomic_save(ledger_path,ledger)
                result['retry_pending']=events.sync_pending(ledger)
                scheduler.atomic_save(RUNTIME/'latest.json',result)
        finally:
            if fd is not None:os.close(fd)
            os.close(own_fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:result=run(args.apply)
    except Exception as exc:
        print(json.dumps(dict(stage='worker',**safe_error(exc)),ensure_ascii=False),flush=True)
        return 75
    print(json.dumps(result,ensure_ascii=False,default=str),flush=True)
    if result.get('retry_pending'):return 75
    return int(bool(result.get('errors')))


if __name__=='__main__':
    raise SystemExit(main())

# поток: fin
from datetime import date
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse
from core import db
from shipment_guard import store,config
router=APIRouter(tags=['Сторож отгрузок'])

@router.get('/shipment-guard',response_class=HTMLResponse)
def page():
    return (Path(__file__).parent/'static/shipment_guard.html').read_text()

@router.get('/api/shipment-guard')
def data(platform: str='',account: str='',since: date|None=None,until: date|None=None,
         signal: str='',limit: int=Query(100,ge=1,le=500)):
    where=['is_current']; args=[]
    if platform: where.append('platform=%s'); args.append(platform)
    if account: where.append('account=%s'); args.append(account)
    if since: where.append("document_moment >= (%s::date::timestamp AT TIME ZONE 'Europe/Moscow')"); args.append(since)
    if until: where.append("document_moment < ((%s::date+1)::timestamp AT TIME ZONE 'Europe/Moscow')"); args.append(until)
    if signal: where.append('signals ? %s'); args.append(signal)
    rows=db.query('SELECT id,demand_id,generation,document_version,result,calculated_at,discovery_source,event_at,received_at FROM ms_shipment_assessments WHERE '+' AND '.join(where)+' ORDER BY calculated_at DESC LIMIT %s',tuple(args+[limit]))
    summary=db.query("""SELECT count(*) FILTER(WHERE state IN ('pending','retry','running')) pending,
      count(*) FILTER(WHERE state='error') errors,
      extract(epoch FROM now()-min(requested_at) FILTER(WHERE state IN ('pending','retry','running'))) oldest_seconds,
      count(*) FILTER(WHERE state='running' AND locked_until<now()) expired_leases FROM ms_shipment_jobs""")[0]
    discovery=db.query("""SELECT discovery_source,count(*) versions FROM ms_shipment_assessments
      WHERE calculated_at>=now()-interval '24 hours' GROUP BY discovery_source""")
    reliability=db.query("""SELECT count(*) notifications,coalesce(sum(duplicate_count),0) duplicates,
      extract(epoch FROM now()-max(received_at)) last_delivery_seconds FROM ms_webhook_inbox""")[0]
    return {'rows':rows,'summary':summary,'metrics':db.query('SELECT * FROM ms_shipment_metrics ORDER BY name'),
      'sync':db.query('SELECT * FROM ms_shipment_sync_state'),'rules':db.query('SELECT * FROM ms_shipment_rules ORDER BY account'),
      'discovery':discovery,'reliability':reliability}

@router.get('/api/shipment-guard/{demand_id}/history')
def history(demand_id:str):
    try: did=str(UUID(demand_id))
    except ValueError: raise HTTPException(400,'invalid_document_id') from None
    return {'rows':db.query('SELECT id,generation,document_version,result,calculated_at,is_current,discovery_source FROM ms_shipment_assessments WHERE demand_id=%s ORDER BY calculated_at DESC LIMIT 100',(did,))}


@router.post('/api/shipment-guard/{demand_id}/retry')
def retry(demand_id:str):
    try: did=str(UUID(demand_id))
    except ValueError: raise HTTPException(400,'invalid_document_id') from None
    with store.transaction() as c:
        c.execute('SELECT 1 FROM ms_shipment_jobs WHERE account_id=%s AND demand_id=%s',(config.account_id(),did))
        if not c.fetchone(): raise HTTPException(404,'unknown_document')
        store.enqueue(c,config.account_id(),did,'manual_recheck')
    return {'status':'queued'}

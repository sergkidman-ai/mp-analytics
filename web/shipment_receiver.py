# поток: fin
"""Isolated receiver: no Basic Auth bypass for dashboard routes; no outbound API calls."""
import json
import logging
import os
import re
import secrets
from uuid import UUID
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response,JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from shipment_guard import config,store,sync
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
MAX_BODY=2*1024*1024

def normalize(payload,account):
    if not isinstance(payload,dict): raise ValueError('payload')
    events=payload.get('events')
    if not isinstance(events,list) or not 1<=len(events)<=1000: raise ValueError('events')
    normalized=[]
    for e in events:
        if not isinstance(e,dict) or str(UUID(e.get('accountId','')))!=account: raise ValueError('account')
        meta=e.get('meta',{})
        if meta.get('type')!='demand' or e.get('action') not in ('CREATE','UPDATE','DELETE'): raise ValueError('type_action')
        href=meta.get('href','')
        pattern=re.escape(config.MS)+'/entity/demand/([0-9a-fA-F-]{36})'
        match=re.fullmatch(pattern,href)
        if not match: raise ValueError('href')
        normalized.append({'demand_id':str(UUID(match[1])),'action':e['action']})
    return normalized

@app.post('/hooks/ms-shipment/{secret}')
async def receive(secret: str, request: Request):
    expected=os.getenv('SHIPMENT_GUARD_WEBHOOK_SECRET','')
    if not expected or not secrets.compare_digest(secret,expected): raise HTTPException(404)
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>MAX_BODY: raise HTTPException(413)
    try:
        request_id=str(UUID(request.query_params.get('requestId','')))
        payload=json.loads(data); account=config.account_id(); events=normalize(payload,account)
        moment=sync.config_time((payload.get('auditContext') or {}).get('moment'))
        metadata={'event_count':len(events),'route':'direct_ms','original_request_id':request_id}
    except (ValueError,TypeError,KeyError,AttributeError): raise HTTPException(400,'invalid_notification') from None
    try:
        await run_in_threadpool(store.accept,account,request_id,payload,events,moment,'webhook',metadata)
    except Exception:
        logging.getLogger("shipment_receiver").critical("notification_not_saved")
        return JSONResponse(status_code=503,content={'detail':'notification_not_saved'},background=BackgroundTask(record_failure))
    return Response(status_code=204)

@app.get('/health')
def health():
    try:
        with store.transaction(fast=True) as c: c.execute('SELECT 1 FROM ms_shipment_jobs LIMIT 1')
    except Exception: raise HTTPException(503,'database_unavailable') from None
    return {'status':'ready'}


def record_failure():
    # Response has already been sent. During DB outage journald keeps the safe error.
    try: store.metric('receiver_storage_errors')
    except Exception: pass

# поток: inv
"""Supply UPDATE webhook: authenticate, durably enqueue, acknowledge immediately."""
import json
import os
import re
import secrets
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ops import ms_supply_queue as queue

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
MAX_BODY = 2 * 1024 * 1024
SUPPLY = re.compile(r'https://api\.moysklad\.ru/api/remap/1\.2/entity/supply/([0-9a-fA-F-]{36})')


def normalize(payload, account):
    if not isinstance(payload, dict):
        raise ValueError('payload')
    events = payload.get('events')
    if not isinstance(events, list) or not 1 <= len(events) <= 1000:
        raise ValueError('events')
    moment = queue.utc((payload.get('auditContext') or {}).get('moment'))
    normalized = []
    for event in events:
        if not isinstance(event, dict) or str(UUID(event.get('accountId', ''))) != account:
            raise ValueError('account')
        meta = event.get('meta') or {}
        match = SUPPLY.fullmatch(meta.get('href', ''))
        if meta.get('type') != 'supply' or event.get('action') != 'UPDATE' or not match:
            raise ValueError('type_action_href')
        fields = event.get('updatedFields')
        if fields is not None:
            if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
                raise ValueError('updated_fields')
            if 'state' not in fields:
                continue
        normalized.append({'receipt_id': str(UUID(match[1])), 'event_timestamp': moment,
                           'updated_fields': fields})
    return normalized


@app.post('/hooks/ms-supply/{secret}')
async def receive(secret: str, request: Request):
    expected = os.getenv('MS_SUPPLY_WEBHOOK_SECRET', '')
    if not expected or not secrets.compare_digest(secret, expected):
        raise HTTPException(404)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise HTTPException(413)
    try:
        request_id = str(UUID(request.query_params.get('requestId', '')))
        account = str(UUID(os.environ['MS_SUPPLY_ACCOUNT_ID']))
        events = normalize(json.loads(data), account)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, 'invalid_notification') from None
    try:
        await run_in_threadpool(queue.accept, account, request_id, events)
    except Exception:
        # Neither exception messages nor the secret-bearing request URL are logged.
        raise HTTPException(503, 'notification_not_saved') from None
    return Response(status_code=204)


@app.get('/health')
def health():
    try:
        if not os.getenv('MS_SUPPLY_WEBHOOK_SECRET'):
            raise ValueError('configuration')
        UUID(os.environ['MS_SUPPLY_ACCOUNT_ID'])
        queue.readiness()
    except Exception:
        raise HTTPException(503, 'storage_unavailable') from None
    return {'status': 'ready'}

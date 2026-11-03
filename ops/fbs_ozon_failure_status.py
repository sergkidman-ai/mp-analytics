# поток: inv
"""Status-only MS feedback for verified Ozon assembly failures; no ship retries."""
from pathlib import Path
from uuid import UUID

import requests

from core import ms_api
from core.ozon_posting_lock import account_lock
from shipment_guard.config import identity
from ops.fbs_market_fulfillment import _save, _now, OZON_READY

TARGET_NAME = 'Не собран в ЛК Озон'
# Resolved by exact name from customerorder metadata on 03.11.2026.
TARGET_ID = '16ed0c1d-c0ff-11ef-0a80-0ca200297efb'
ALLOWED_SOURCE = {'Собирается', 'Подтвержден', 'В пути'}


def _candidate(order, result, folder):
    legacy_ack = (result.get('result') == 'ready' and result.get('reason') in
                  ('ozon_ship_api_confirmed', 'saved_ozon_ship_api_confirmed'))
    if (result.get('platform') != 'ozon' or (result.get('result') not in ('blocked', 'uncertain') and not legacy_ack)
            or result.get('order_id') != order.get('id') or result.get('number') != order.get('name')
            or identity(order)[:2] != ('ozon', result.get('account'))):
        return False
    reason = result.get('reason', '')
    if any(x in reason for x in ('identity_mismatch', 'checkpoint_identity', 'scope_mismatch')):
        return False
    return (legacy_ack or 'gtd' in reason or reason.startswith('exemplar_')
            or (folder/'mut_ship_intent.json').exists()
            or reason in ('ozon_ship_rejected', 'ozon_still_awaiting_packaging',
                          'mutation_not_confirmed_no_repeat', 'prior_reconciliation_no_write_repeat',
                          'point_reconciliation_failed_no_repeat'))


def sync_failure_status(order, folder, result, session, *, get=None, request=None):
    """Both scheduled and receipt-driven callers use this after the MP adapter."""
    folder = Path(folder)
    if not _candidate(order, result, folder):
        return result
    get, request = get or ms_api.get, request or requests.request
    feedback = {}
    try:
        UUID(str(order['id']))
        with account_lock(result['account'], timeout=5) as lease:
            # A saved error is not evidence that the posting is still unassembled.
            current = session._ozon_get(folder, result, lease, 'ozon_ms_status_check')
            state = current.get('status')
            if state in OZON_READY:
                value = dict(result, result='already_ready', reason='ozon_fresh_status_confirmed', status=state)
                _save(folder/'result.json', value)
                return value
            if state != 'awaiting_packaging':
                return result
            if result.get('result') == 'ready':
                result = dict(result, result='blocked', reason='ozon_still_awaiting_packaging', status=state)
            path = '/entity/customerorder/' + order['id']
            actual = get(path, {'expand':'state,organization,agent'})
            if (actual.get('id') != order['id'] or actual.get('name') != order['name']
                    or identity(actual)[:2] != ('ozon', result['account'])):
                raise ValueError('ms_order_identity_changed')
            if ms_api.meta_id(actual, 'state') == TARGET_ID:
                feedback = {'result':'already_marked', 'state':TARGET_NAME}
            elif actual.get('state', {}).get('name') not in ALLOWED_SOURCE:
                feedback = {'result':'unchanged', 'reason':'ms_order_state_changed'}
            elif (folder/'ms_ozon_failure_intent.json').exists():
                feedback = {'result':'uncertain', 'reason':'prior_ms_status_intent_no_repeat'}
            else:
                payload = {'state':{'meta':{
                    'href':ms_api.BASE+'/entity/customerorder/metadata/states/'+TARGET_ID,
                    'type':'state', 'mediaType':'application/json'}}}
                _save(folder/'ms_ozon_failure_intent.json', {
                    'at':_now(), 'order_id':order['id'], 'number':order['name'],
                    'before_state':actual.get('state'), 'before_updated':actual.get('updated'),
                    'market_status':state, 'market_reason':result.get('reason'), 'payload':payload}, exclusive=True)
                lease.check()
                try:
                    response = request('PUT', ms_api.BASE+path, json=payload,
                        headers={'Authorization':'Bearer '+ms_api.TOKEN, 'Content-Type':'application/json'},
                        timeout=(5,30), allow_redirects=False)
                    _save(folder/'ms_ozon_failure_response.json', {'at':_now(), 'http':response.status_code})
                except requests.RequestException as exc:
                    _save(folder/'ms_ozon_failure_transport.json', {'at':_now(),'error':type(exc).__name__})
                after = get(path, {'expand':'state,organization,agent'})
                verified = (after.get('id') == order['id'] and after.get('name') == order['name']
                            and identity(after)[:2] == ('ozon', result['account'])
                            and ms_api.meta_id(after,'state') == TARGET_ID)
                feedback = {'result':'updated' if verified else 'uncertain', 'state':TARGET_NAME,
                            'verified':verified}
    except Exception as exc:
        feedback = {'result':'error', 'reason':type(exc).__name__+':'+str(exc)[:160]}
    _save(folder/'ms_ozon_failure_result.json', {'at':_now(),**feedback})
    value = dict(result, ms_status_feedback=feedback)
    _save(folder/'result.json', value)
    return value

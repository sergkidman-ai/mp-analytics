# поток: inv
"""Finish today's existing MS shipments on marketplaces without creating documents."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from core import ms_api
from ops import fbs_ms_post_import_shipments as shipments
from ops.fbs_market_fulfillment import fulfill_many
from ops.fbs_ms_post_import_status import STATE
from shipment_guard.config import identity

RUNTIME = Path(__file__).resolve().parents[1] / 'runtime/fbs_ready_market_catchup'
PAGE_SIZE = 20
MAX_ORDERS = 20
RUN_SECONDS = 480
EXPAND = 'state,store,organization,agent,positions.assortment'
SUPPORTED = {('wb', 'wb_acc1'), ('wb', 'wb_acc2'), ('ozon', 'oz_acc1'),
             ('ozon', 'oz_acc2'), ('yandex', 'ya_acc1')}


def now():
    return datetime.now(timezone.utc)


def read(path):
    return json.loads(path.read_text())


def scan(day):
    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    offset, result, seen = 0, [], set()
    deadline = time.monotonic() + RUN_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError('catchup_scope_time_budget')
        page = ms_api.get('/entity/customerorder', {
            'limit': PAGE_SIZE, 'offset': offset, 'order': 'id,asc', 'expand': EXPAND,
            'filter': ['state=' + ms_api.BASE + '/entity/customerorder/metadata/states/' + STATE['Собирается'],
                       'deliveryPlannedMoment>=' + day + ' 00:00:00',
                       'deliveryPlannedMoment<' + next_day + ' 00:00:00']})
        values = page.get('rows', [])
        total = page.get('meta', {}).get('size')
        if not isinstance(total, int) or not values and offset < total:
            raise ValueError('catchup_scope_incomplete')
        for order in values:
            if order['id'] in seen:
                raise ValueError('catchup_duplicate_identity')
            seen.add(order['id'])
            if (order.get('state', {}).get('name') != 'Собирается'
                    or str(order.get('deliveryPlannedMoment', ''))[:10] != day):
                raise ValueError('catchup_scope_mismatch')
            if identity(order)[:2] in SUPPORTED:
                result.append(order)
        offset += len(values)
        if offset >= total:
            return result


def verified_order(snapshot, day):
    order = ms_api.get('/entity/customerorder/' + snapshot['id'], {'expand': EXPAND})
    if (order.get('id') != snapshot['id'] or order.get('name') != snapshot['name']
            or identity(order)[:2] != identity(snapshot)[:2]
            or order.get('state', {}).get('name') != 'Собирается'
            or str(order.get('deliveryPlannedMoment', ''))[:10] != day):
        raise ValueError('catchup_order_changed')
    refs = order.get('demands', [])
    if len(refs) != 1:
        raise ValueError('catchup_requires_one_existing_demand')
    demand_id = ms_api.meta_id({'demand': refs[0]}, 'demand')
    demand = ms_api.get('/entity/demand/' + demand_id, {'expand': 'positions.assortment'})
    if demand.get('id') != demand_id or not shipments.demand_matches(demand, order):
        raise ValueError('catchup_demand_mismatch')
    fresh = ms_api.get('/entity/customerorder/' + order['id'], {'expand': EXPAND})
    if fresh != order:
        raise ValueError('catchup_order_changed_during_verification')
    return fresh, demand_id


def retry_transient_read(folder):
    """Only reopen failed reads; mutation evidence is never removed or retried."""
    path = folder / 'result.json'
    if not path.exists() or list(folder.glob('mut_*_intent.json')):
        return
    saved = read(path)
    reason = str(saved.get('reason', ''))
    transient = (reason in {'read_api_http_429', 'read_api_http_500', 'read_api_http_502',
                            'read_api_http_503', 'read_api_http_504', 'ozon_account_lock_unavailable'}
                 or '_transport_' in reason or reason.startswith('ozon_package_gate_failed_'))
    if saved.get('result') == 'blocked' and transient:
        with (folder / 'fulfillment.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not list(folder.glob('mut_*_intent.json')) and read(path) == saved:
                path.rename(folder / ('read_failure_' + now().strftime('%Y%m%dT%H%M%S%f') + '.json'))


def _run(apply=False):
    deadline = time.monotonic() + RUN_SECONDS
    day = now().astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNTIME / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        orders = scan(day)
        folder = shipments.RUNTIME / 'market'
        candidates = []
        for order in orders:
            result_path = folder / order['id'] / 'result.json'
            if result_path.exists():
                saved = read(result_path)
                if (saved.get('order_id') == order['id'] and saved.get('number') == order['name']
                        and saved.get('result') in shipments.MARKET_READY):
                    continue
            attempt = RUNTIME / order['id'] / 'latest.json'
            candidates.append((read(attempt).get('at', '') if attempt.exists() else '', order))
        candidates.sort(key=lambda item: (item[0], item[1]['id']))
        results, verified = [], []
        for _, order in candidates[:MAX_ORDERS]:
            if time.monotonic() >= deadline:
                break
            item = {'at': now().isoformat(), 'id': order['id'], 'number': order['name']}
            try:
                fresh, demand_id = verified_order(order, day)
                item.update(result='verified', demand_id=demand_id)
                if apply:
                    retry_transient_read(folder / fresh['id'])
                    verified.append(fresh)
            except Exception as exc:
                item.update(result='deferred', reason=type(exc).__name__ + ':' + str(exc)[:120])
            results.append(item)
        if apply and verified:
            markets = fulfill_many(verified, folder)
            for item in results:
                if item['result'] == 'verified':
                    market = markets.get(item['id'], {})
                    if market.get('order_id') != item['id'] or market.get('number') != item['number']:
                        item.update(result='deferred', reason='catchup_market_identity_mismatch')
                    else:
                        item.update(result=market.get('result', 'uncertain'), market=market)
        if apply:
            for item in results:
                shipments.durable_save(RUNTIME / item['id'] / 'latest.json', item)
        summary = {'date': day, 'apply': apply, 'scanned': len(orders),
                   'pending': len(candidates), 'selected': len(results),
                   'ready': sum(item['result'] in shipments.MARKET_READY for item in results),
                   'results': results, 'stock_checks': 0, 'demands_created': 0}
        if apply:
            shipments.durable_save(RUNTIME / 'latest.json', summary)
        return summary


def run(apply=False):
    previous = ms_api.TIMEOUT, ms_api.RETRIES
    ms_api.TIMEOUT, ms_api.RETRIES = 30, 2
    try:
        return _run(apply)
    finally:
        ms_api.TIMEOUT, ms_api.RETRIES = previous


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), ensure_ascii=False))

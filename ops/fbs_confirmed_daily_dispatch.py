# поток: inv
"""Dispatch today's confirmed MoySklad customer orders every twenty minutes.

The selection is exactly state=Подтвержден and deliveryPlannedMoment=today.
No stock, reserve, supplier, or availability checks are made. Each selected
order is moved to Собирается, its MoySklad demand is created or reconciled,
then the supported marketplace is moved to ready-to-ship. Every write has a
durable intent; uncertain writes are never blindly repeated.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import requests
import sys
from zoneinfo import ZoneInfo

from core import ms_api
from ops import fbs_market_fulfillment as market
from ops import fbs_ms_post_import_shipments as shipments
from ops import fbs_ms_post_import_status as status
from shipment_guard.config import identity

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime' / 'fbs_confirmed_daily_dispatch'
MSK = ZoneInfo('Europe/Moscow')
EXPAND = 'state,store,organization,agent,positions.assortment'
PAGE_SIZE = 100
ORDER_LIMIT = 20
ONE_TIME_EXCLUSIONS = {'2026-11-04'}
READY_RESULTS = {'ready', 'already_ready'}
DEMAND_RESULTS = {'created', 'created_resolved', 'existing'}


def now() -> datetime:
    return datetime.now(timezone.utc)


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def save(path: Path, value: dict, *, exclusive: bool = False) -> None:
    shipments.durable_save(path, value, exclusive=exclusive)


def local_date(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(MSK)
    return parsed.date().isoformat()


def rows(order: dict) -> list[dict]:
    positions = order.get('positions')
    if not isinstance(positions, dict):
        raise ValueError('customer_order_positions_missing')
    values = positions.get('rows') or []
    if positions.get('meta', {}).get('size', len(values)) != len(values):
        raise ValueError('customer_order_positions_incomplete')
    if not values:
        raise ValueError('customer_order_positions_empty')
    return values


def states() -> dict[str, str]:
    payload = ms_api.get('/entity/customerorder/metadata')
    result = {}
    for name in ('Подтвержден', 'Собирается'):
        matches = [item for item in payload.get('states', []) if item.get('name') == name]
        if len(matches) != 1 or not matches[0].get('id'):
            raise ValueError('customer_order_state_metadata_' + name)
        result[name] = str(matches[0]['id'])
    return result


def scan(day: str) -> list[dict]:
    ids = states()
    start = date.fromisoformat(day)
    next_day = start + timedelta(days=1)
    filters = [
        'state=' + ms_api.BASE + '/entity/customerorder/metadata/states/' + ids['Подтвержден'],
        'deliveryPlannedMoment>=' + day + ' 00:00:00',
        'deliveryPlannedMoment<' + next_day.isoformat() + ' 00:00:00',
    ]
    result, seen_ids, seen_names = [], set(), set()
    offset, total = 0, None
    while total is None or offset < total:
        payload = ms_api.get('/entity/customerorder', {
            'limit': PAGE_SIZE, 'offset': offset, 'filter': filters, 'expand': EXPAND,
        })
        page = payload.get('rows') or []
        size = payload.get('meta', {}).get('size')
        if not isinstance(size, int) or (not page and offset < size):
            raise ValueError('customer_order_scope_incomplete')
        if total is not None and total != size:
            raise ValueError('customer_order_scope_changed_during_scan')
        total = size
        for order in page:
            if order.get('id') in seen_ids or order.get('name') in seen_names:
                raise ValueError('duplicate_customer_order_identity_in_scope')
            if (order.get('state', {}).get('name') != 'Подтвержден'
                    or local_date(order.get('deliveryPlannedMoment')) != day):
                raise ValueError('customer_order_scope_filter_mismatch')
            rows(order)
            platform, account, *_ = identity(order)
            result.append({
                'id': order['id'], 'name': order['name'],
                'organization_id': ms_api.meta_id(order, 'organization'),
                'platform': platform, 'account': account,
                'moment': order.get('moment'), 'planned': order.get('deliveryPlannedMoment'),
            })
            seen_ids.add(order['id'])
            seen_names.add(order['name'])
        offset += len(page)
        if offset > 20000:
            raise ValueError('customer_order_scope_limit_exceeded')
    return sorted(result, key=lambda item: (item.get('moment') or '', item['name'], item['id']))


def scope_for(day: str) -> tuple[Path, dict]:
    folder = RUNTIME / day
    scope_path = folder / 'scope.json'
    if scope_path.exists():
        saved = read(scope_path)
        if saved.get('day') != day or not isinstance(saved.get('orders'), list):
            raise ValueError('daily_dispatch_scope_identity_mismatch')
        fresh = scan(day)
        known = {order['id'] for order in saved['orders']}
        additions = [order for order in fresh if order['id'] not in known]
        if additions:
            saved['orders'].extend(additions)
            saved['refreshed_at'] = now().isoformat()
            # Invalidate before saving: a crash must not leave new orders marked complete.
            (folder / 'complete.json').unlink(missing_ok=True)
            save(scope_path, saved)
        return folder, saved
    orders = scan(day)
    value = {'day': day, 'created_at': now().isoformat(), 'orders': orders}
    save(scope_path, value, exclusive=True)
    return folder, value


def current(order_ref: dict) -> dict:
    order = ms_api.get('/entity/customerorder/' + order_ref['id'], {'expand': EXPAND})
    if order.get('id') != order_ref['id'] or order.get('name') != order_ref['name']:
        raise ValueError('customer_order_identity_changed')
    return order


def state_after(order: dict, order_ref: dict, folder: Path, day: str, ids: dict[str, str]) -> tuple[dict | None, dict]:
    """Apply/reconcile exactly one status PUT, without an inventory lookup."""
    proof_path = folder / 'status_proof.json'
    if proof_path.exists():
        proof = read(proof_path)
        latest = current(order_ref)
        if (latest.get('state', {}).get('name') == 'Собирается'
                and local_date(latest.get('deliveryPlannedMoment')) == day
                and proof.get('id') == latest.get('id')):
            proof['status_response'] = latest
            save(proof_path, proof)
            return latest, proof
        return None, {'result': 'blocked', 'reason': 'status_changed_after_saved_proof'}

    intent_path = folder / 'status_intent.json'
    if intent_path.exists():
        intent = read(intent_path)
        if (intent.get('id') != order_ref['id'] or intent.get('name') != order_ref['name']
                or intent.get('day') != day):
            return None, {'result': 'blocked', 'reason': 'prior_status_intent_identity_mismatch'}
        latest = current(order_ref)
        if (latest.get('state', {}).get('name') != 'Собирается'
                or local_date(latest.get('deliveryPlannedMoment')) != day):
            return None, {'result': 'uncertain', 'reason': 'prior_status_put_not_confirmed_no_retry'}
    else:
        if order.get('state', {}).get('name') != 'Подтвержден':
            return None, {'result': 'skipped', 'reason': 'state_no_longer_confirmed'}
        if local_date(order.get('deliveryPlannedMoment')) != day:
            return None, {'result': 'skipped', 'reason': 'planned_date_changed'}
        target_href = (ms_api.BASE + '/entity/customerorder/metadata/states/' + ids['Собирается'])
        body = {'state': {'meta': {'href': target_href, 'type': 'state',
                                   'mediaType': 'application/json'}}}
        save(intent_path, {'at': now().isoformat(), 'id': order['id'], 'name': order['name'],
                           'day': day, 'before_updated': order.get('updated'),
                           'method': 'PUT', 'body': body}, exclusive=True)
        response_data = None
        try:
            response = requests.put(ms_api.BASE + '/entity/customerorder/' + order['id'],
                headers={'Authorization': 'Bearer ' + ms_api.TOKEN,
                         'Content-Type': 'application/json', 'Accept-Encoding': 'gzip'},
                json=body, timeout=(5, 45), allow_redirects=False)
            try:
                response_data = response.json() if response.content else None
            except ValueError:
                response_data = {'non_json_response': True}
            save(folder / 'status_response.json', {'http': response.status_code,
                                                    'body': response_data})
        except requests.RequestException as exc:
            save(folder / 'status_transport.json', {'error': type(exc).__name__})
        latest = current(order_ref)
        if (latest.get('state', {}).get('name') != 'Собирается'
                or local_date(latest.get('deliveryPlannedMoment')) != day):
            return None, {'result': 'uncertain', 'reason': 'status_put_not_verified_no_retry'}

    if latest.get('state', {}).get('name') != 'Собирается' or local_date(latest.get('deliveryPlannedMoment')) != day:
        return None, {'result': 'uncertain', 'reason': 'status_readback_mismatch'}
    values = rows(latest)
    proof = {
        'id': latest['id'], 'name': latest['name'], 'sum': latest['sum'],
        'planned': latest.get('deliveryPlannedMoment'),
        'positions': [(ms_api.meta_id(position, 'assortment'), str(position['quantity']))
                      for position in values],
        'state': 'Собирается', 'status_response': latest,
    }
    save(proof_path, proof)
    return latest, proof


def process_order(order_ref: dict, day: str, ids: dict[str, str], job_folder: Path) -> dict:
    folder = job_folder / 'orders' / order_ref['id']
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    result_path = folder / 'result.json'
    if result_path.exists():
        previous = read(result_path)
        if previous.get('complete') is True:
            return previous
    try:
        before = current(order_ref)
        after, proof_or_error = state_after(before, order_ref, folder, day, ids)
        if after is None:
            result = {'id': order_ref['id'], 'name': order_ref['name'],
                      'result': proof_or_error['result'], 'reason': proof_or_error['reason']}
            save(result_path, result)
            return result
        proof = proof_or_error
        # Only document identity/composition is checked by the existing shipment adapter.
        # No stock or reserve API is called.
        demand_rows = shipments.demand_batches([after])
        org_href = after['organization']['meta']['href']
        demand = shipments.apply_one(after, proof, demand_rows.get((org_href, after['name']), []), True)
        demand['id'] = after['id']
        demand['status'] = 'Собирается'
        if demand.get('result') not in DEMAND_RESULTS:
            result = {**demand, 'complete': False}
            save(result_path, result)
            return result

        platform, account, *_ = identity(after)
        if platform == 'direct':
            market_result = {'result': 'not_applicable', 'reason': 'order_has_no_marketplace_agent'}
        else:
            market_folder = shipments.RUNTIME / 'market' / after['id']
            market_result = market.fulfill(after, market_folder)
        demand['market'] = market_result
        demand['market_due'] = False
        demand['complete'] = (platform == 'direct' or market_result.get('result') in READY_RESULTS)
        save(shipments.RUNTIME / after['name'] / 'result.json', {'at': now().isoformat(), **demand})
        save(result_path, demand)
        return demand
    except Exception as exc:
        result = {'id': order_ref['id'], 'name': order_ref['name'],
                  'result': 'error', 'reason': type(exc).__name__ + ':' + str(exc)[:160],
                  'complete': False}
        save(result_path, result)
        return result


def process_scope(day: str, folder: Path, scope: dict) -> dict:
    existing_summary = folder / 'complete.json'
    if existing_summary.exists():
        return read(existing_summary)
    ids = states()
    results = []
    for start in range(0, len(scope['orders']), ORDER_LIMIT):
        batch_refs = scope['orders'][start:start + ORDER_LIMIT]
        for order_ref in batch_refs:
            results.append(process_order(order_ref, day, ids, folder))
    # One order per call avoids collisions if organizations reuse a document number.
    # The adapter handles its own per-market rate limits and durable mutation proofs.
    final = []
    for result in results:
        if result.get('complete') is True or result.get('result') in {'blocked', 'skipped', 'uncertain', 'error'}:
            final.append(result)
            continue
        # A demand-stage success without a marketplace checkpoint can be safely resumed
        # by the per-order result file on the next execution.
        final.append(result)
    counts = Counter(item.get('result', 'unknown') for item in final)
    market_ready = sum(item.get('market', {}).get('result') in READY_RESULTS for item in final)
    not_applicable = sum(item.get('market', {}).get('result') == 'not_applicable' for item in final)
    unresolved = sum(item.get('complete') is not True for item in final)
    summary = {'date': day, 'finished_at': now().isoformat(), 'selected': len(scope['orders']),
               'status_assembling': sum(item.get('status') == 'Собирается' for item in final),
               'demands_created': sum(item.get('result') in {'created', 'created_resolved'} for item in final),
               'demands_existing': sum(item.get('result') == 'existing' for item in final),
               'market_ready': market_ready, 'market_not_applicable': not_applicable,
               'unresolved': unresolved, 'result_counts': dict(counts)}
    save(folder / 'summary.json', summary)
    if unresolved == 0:
        save(existing_summary, summary, exclusive=True)
    return summary


def run(apply: bool = False) -> dict:
    local = now().astimezone(MSK)
    day = local.date().isoformat()
    if local.weekday() == 6:
        return {'date': day, 'skipped': 'sunday'}
    if day in ONE_TIME_EXCLUSIONS:
        return {'date': day, 'skipped': 'one_time_exclusion'}
    if not apply:
        selected = scan(day)
        by_market = Counter(item['platform'] for item in selected)
        return {'date': day, 'dry_run': True, 'selected': len(selected),
                'by_platform': dict(by_market), 'stock_checks': 0}

    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    lock_path = RUNTIME / 'lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Resume unfinished scopes first; no new candidates are added to an old snapshot.
        for scope_path in sorted(RUNTIME.glob('*/scope.json')):
            prior_day = scope_path.parent.name
            if prior_day < day and not (scope_path.parent / 'complete.json').exists():
                saved = read(scope_path)
                process_scope(prior_day, scope_path.parent, saved)
        folder, scope = scope_for(day)
        summary = process_scope(day, folder, scope)
        return summary
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='perform MoySklad and marketplace writes')
    args = parser.parse_args()
    result = run(apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, default=str))
    raise SystemExit(1 if result.get('unresolved', 0) else 0)

# поток: inv
"""Create one demand, then finish its marketplace stage after a shared delay.

Only explicit status-step shipment_due proofs enter this queue. The existing
batch adapter supplies templates, composition checks and single-attempt writes.
Availability is inherited from the importer; no stock/reserve API is called.
Only verified MS shipments enter the existing marketplace adapter. Unfinished
market steps survive a restart; blocked/uncertain mutations require resolution.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from core import ms_api
from ops.order_priority import order_key
from ops.fbs_market_fulfillment import fulfill_many
from ops import fbs_ms_post_import_status as status
from tools import customer_order_batch17_20261022 as batch

STATUS_RUNTIME = status.RUNTIME
RUNTIME = STATUS_RUNTIME / 'shipment'
MAX_ORDERS = 10
WAIT_SECONDS = 300
POLL_SECONDS = 30
ORDER_PAUSE = 1
EXPAND = 'state,store,positions.assortment'
TERMINAL = {'created', 'created_resolved', 'existing', 'skipped', 'pending_manual'}
SHIPPED = {'created', 'created_resolved', 'existing'}
MARKET_READY = {'ready', 'already_ready'}


def now() -> datetime:
    return datetime.now(timezone.utc)


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def durable_save(path: Path, value: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    target = path if exclusive else path.with_name(path.name + '.tmp')
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        os.replace(target, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def changed_at(proof: dict) -> datetime:
    value = datetime.fromisoformat(proof['changed_at'])
    if value.tzinfo is None:
        raise ValueError('shipment_changed_at_missing_timezone')
    return value.astimezone(timezone.utc)


def priority_key(proof: dict):
    # Old status journals contain the full MS response, so existing queues also
    # get correct priority without additional MS/stock requests.
    fallback={}
    path=STATUS_RUNTIME/proof['name']/'response.json'
    if not proof.get('moment') or 'agent_id' not in proof:
        if path.exists():
            response=read(path)
            if isinstance(response.get('body'),dict):fallback=response['body']
    return order_key(proof,fallback)


def pending() -> list[dict]:
    result = []
    for path in STATUS_RUNTIME.glob('*/readback.json'):
        proof = read(path)
        if proof.get('shipment_due') is not True or proof.get('state') != 'Собирается':
            continue
        number = proof['name']
        if number != path.parent.name:
            raise ValueError('shipment_proof_folder_identity')
        previous = RUNTIME / number / 'result.json'
        if previous.exists():
            saved = read(previous)
            # Legacy vatSum failures occurred before any creation attempt.
            # Recover those after the template fix, never uncertain POSTs.
            retry_template = (saved.get('result') == 'pending_manual'
                              and saved.get('reason') == 'template_vat_mismatch'
                              and not (previous.parent / 'create_guard.json').exists()
                              and not (previous.parent / 'create_intent.json').exists())
            retry_market = (saved.get('result') in SHIPPED
                            and saved.get('market_due') is True)
            if saved.get('result') in TERMINAL and not (retry_template or retry_market):
                continue
        changed_at(proof)
        result.append(proof)
    return sorted(result, key=priority_key)


def wait_until(deadline: datetime) -> None:
    while True:
        remaining = (deadline - now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(POLL_SECONDS, remaining))


def rows(document: dict) -> list[dict]:
    positions = document['positions']
    if isinstance(positions, list):
        return positions
    values = positions['rows']
    if positions.get('meta', {}).get('size', len(values)) != len(values):
        raise ValueError('shipment_positions_incomplete')
    return values


def quantities(values: list[dict]) -> Counter:
    result = Counter()
    for position in values:
        result[ms_api.meta_id(position, 'assortment')] += Decimal(str(position['quantity']))
    return result


def unchanged(order: dict, proof: dict, after: dict) -> str | None:
    if order.get('id') != proof['id'] or order.get('name') != proof['name']:
        return 'order_identity_changed'
    if order.get('state', {}).get('name') != 'Собирается':
        return 'order_state_changed'
    if after.get('id') != proof['id'] or after.get('name') != proof['name']:
        return 'status_response_identity_mismatch'
    for key in ('store', 'organization', 'agent'):
        if not ms_api.meta_id(order, key) or ms_api.meta_id(order, key) != ms_api.meta_id(after, key):
            return key + '_changed'
    if Decimal(str(order['sum'])) != Decimal(str(proof['sum'])) or order['sum'] != after['sum']:
        return 'order_sum_changed'
    if order.get('deliveryPlannedMoment') != proof.get('planned'):
        return 'order_plan_changed'
    actual = Counter((ms_api.meta_id(position, 'assortment'), Decimal(str(position['quantity'])))
                     for position in rows(order))
    saved = Counter((str(cid), Decimal(str(quantity))) for cid, quantity in proof['positions'])
    if actual != saved or batch.signature(rows(order)) != batch.signature(rows(after)):
        return 'order_positions_changed'
    return None


def current_orders(proofs: list[dict]) -> dict[str, dict]:
    numbers = [proof['name'] for proof in proofs]
    payload = ms_api.get('/entity/customerorder', {
        'limit': MAX_ORDERS, 'filter': ['name=' + number for number in numbers], 'expand': EXPAND})
    found = payload.get('rows', [])
    if payload.get('meta', {}).get('size', len(found)) != len(found):
        raise ValueError('shipment_order_batch_incomplete_or_duplicate')
    result = {}
    for order in found:
        number = order['name']
        if number not in numbers or number in result:
            raise ValueError('shipment_order_batch_unexpected_or_duplicate')
        result[number] = order
    return result


def demand_batches(orders: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups = {}
    for order in orders:
        groups.setdefault(order['organization']['meta']['href'], []).append(order['name'])
    result = {}
    for organization, numbers in groups.items():
        payload = ms_api.get('/entity/demand', {
            'limit': 20, 'filter': ['organization=' + organization] + ['name=' + number for number in numbers],
            'expand': 'positions.assortment'})
        found = payload.get('rows', [])
        if payload.get('meta', {}).get('size', len(found)) != len(found):
            raise ValueError('shipment_demand_batch_incomplete')
        for demand in found:
            if demand.get('name') not in numbers or demand.get('organization', {}).get('meta', {}).get('href') != organization:
                raise ValueError('shipment_demand_batch_unexpected')
            result.setdefault((organization, demand['name']), []).append(demand)
    return result


def demand_matches(demand: dict, order: dict) -> bool:
    try:
        rows(demand)
        return bool(demand.get('id') and demand.get('name') == order['name']
                    and all(demand.get(key) == order.get(key)
                            for key in ('vatSum', 'vatIncluded', 'vatEnabled'))
                    and batch.demand_check(demand, order))
    except (KeyError, TypeError, ValueError):
        return False


def resolve_once(order: dict, folder: Path) -> dict | None:
    """One narrow fact query after an uncertain POST; never repeat the POST."""
    payload = ms_api.get('/entity/demand', {
        'limit': 2, 'filter': ['name=' + order['name'],
                             'organization=' + order['organization']['meta']['href']],
        'expand': 'positions.assortment'})
    durable_save(folder / 'resolve.json', payload)
    found = payload.get('rows', [])
    if payload.get('meta', {}).get('size', len(found)) == 1 and len(found) == 1 and demand_matches(found[0], order):
        return found[0]
    return None


def apply_one(order: dict, proof: dict, existing: list[dict], apply: bool) -> dict:
    number = proof['name']
    folder = RUNTIME / number
    source = STATUS_RUNTIME / number
    after = (proof['status_response'] if 'status_response' in proof
             else read(source / 'response.json')['body'])
    reason = unchanged(order, proof, after)
    if reason:
        return {'number': number, 'result': 'skipped', 'reason': reason}
    refs = order.get('demands', [])
    if len(refs) > 1 or len(existing) > 1:
        return {'number': number, 'result': 'pending_manual', 'reason': 'multiple_demands'}
    if existing:
        demand = existing[0]
        linked_ids = [ms_api.meta_id({'ref': ref}, 'ref') for ref in refs]
        if demand_matches(demand, order) and (not refs or linked_ids == [demand['id']]):
            return {'number': number, 'result': 'existing', 'demand_id': demand['id']}
        return {'number': number, 'result': 'pending_manual', 'reason': 'same_number_demand_mismatch'}
    if refs:
        return {'number': number, 'result': 'pending_manual', 'reason': 'linked_demand_number_or_org_mismatch'}
    if (folder / 'create_guard.json').exists() or (folder / 'create_intent.json').exists():
        return {'number': number, 'result': 'pending_manual', 'reason': 'prior_creation_intent_no_retry'}
    if not apply:
        return {'number': number, 'result': 'would_create'}
    durable_save(folder / 'order_before.json', order)
    template = ms_api.put('/entity/demand/new', {'customerOrder': {'meta': order['meta']}})
    durable_save(folder / 'template.json', template)
    positions = rows(template)
    if batch.signature(positions) != batch.signature(rows(order)):
        return {'number': number, 'result': 'pending_manual', 'reason': 'template_positions_mismatch'}
    for key in ('store', 'organization', 'agent'):
        if ms_api.meta_id(template, key) != ms_api.meta_id(order, key):
            return {'number': number, 'result': 'pending_manual', 'reason': 'template_' + key + '_mismatch'}
    # MS calculates vatSum on the saved document; /demand/new may omit it.
    # demand_matches still verifies vatSum on the actual POST response.
    if any(template.get(key) != order.get(key) for key in ('vatIncluded', 'vatEnabled')):
        return {'number': number, 'result': 'pending_manual', 'reason': 'template_vat_mode_mismatch'}
    if ms_api.meta_id(template, 'customerOrder') != order['id']:
        return {'number': number, 'result': 'pending_manual', 'reason': 'template_order_mismatch'}
    payload = {key: value for key, value in template.items()
               if key not in {'sum', 'payedSum', 'printed', 'published', 'files'}}
    payload.update(positions=positions, name=number, applicable=True,
                   moment=now().astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S'))
    durable_save(folder / 'create_guard.json', {'at': now().isoformat(), 'number': number,
                 'order_id': order['id'], 'availability': 'inherited_from_import',
                 'method': 'POST', 'path': '/entity/demand', 'body': payload}, exclusive=True)
    try:
        response = batch.one_write(folder, 'create', 'POST', '/entity/demand?expand=positions.assortment', payload)
    except Exception as exc:
        response = {'uncertain': True, 'exception': type(exc).__name__}
    durable_save(folder / 'response.json', response)
    if response.get('status') in (200, 201) and demand_matches(response.get('body') or {}, order):
        return {'number': number, 'result': 'created', 'demand_id': response['body']['id']}
    if response.get('uncertain') or response.get('status', 0) >= 500:
        try:
            actual = resolve_once(order, folder)
        except Exception as exc:
            durable_save(folder / 'resolve_error.json', {'error': type(exc).__name__})
            actual = None
        if actual:
            return {'number': number, 'result': 'created_resolved', 'demand_id': actual['id']}
        return {'number': number, 'result': 'pending_manual', 'reason': 'creation_uncertain_no_retry'}
    return {'number': number, 'result': 'pending_manual', 'reason': 'creation_response_not_verified',
            'http': response.get('status')}


def finish_market(orders: list[dict], results: list[dict]) -> None:
    """Resume only journalled shipments; marketplace intents live per MS ID."""
    if not orders:
        return
    orders = sorted(orders,key=order_key)
    error = None
    try:
        market_results = fulfill_many(orders, RUNTIME / 'market')
        if not isinstance(market_results, dict):
            raise ValueError('invalid_market_batch_result')
    except Exception as exc:
        # The adapter may already have completed part of the batch.
        # Preserve its durable results and never blindly repeat a mutation.
        market_results, error = {}, type(exc).__name__
    by_number = {result['number']: result for result in results}
    for order in orders:
        market = market_results.get(order['id'])
        evidence = RUNTIME / 'market' / order['id'] / 'result.json'
        if market is None and evidence.exists():
            market = read(evidence)
        if (not isinstance(market, dict) or market.get('order_id') != order['id']
                or market.get('number') != order['name']):
            market = {'order_id': order['id'], 'number': order['name'],
                      'result': 'uncertain', 'reason': error or 'market_result_not_verified'}
        result = by_number[order['name']]
        result.update(market=market, market_due=False)
        durable_save(RUNTIME / order['name'] / 'result.json', {'at': now().isoformat(), **result})


def run(apply: bool) -> dict:
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    fd = os.open(RUNTIME / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
    previous_limits = ms_api.TIMEOUT, ms_api.RETRIES
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ms_api.TIMEOUT, ms_api.RETRIES = 45, 2
        queue = pending()
        selected = queue[:MAX_ORDERS]
        results, market_orders = [], []
        deadline = max((changed_at(proof) for proof in selected), default=now()) + timedelta(seconds=WAIT_SECONDS)
        if selected and apply:
            wait_until(deadline)
        if selected and (apply or now() >= deadline):
            orders = current_orders(selected)
            demands = demand_batches(list(orders.values())) if orders else {}
            for index, proof in enumerate(selected):
                order = orders.get(proof['name'])
                try:
                    if order is None:
                        result = {'number': proof['name'], 'result': 'skipped', 'reason': 'order_missing'}
                    else:
                        key = (order['organization']['meta']['href'], proof['name'])
                        result = apply_one(order, proof, demands.get(key, []), apply)
                except Exception as exc:
                    result = {'number': proof['name'], 'result': 'error',
                              'reason': type(exc).__name__ + ':' + str(exc)[:160]}
                results.append(result)
                if apply:
                    if result.get('result') in SHIPPED and result.get('demand_id') and order:
                        # Commit the MS result before touching the marketplace.
                        # A crash here requeues only this unfinished market step.
                        result['market_due'] = True
                        market_orders.append(order)
                    durable_save(RUNTIME / proof['name'] / 'result.json', {'at': now().isoformat(), **result})
                    durable_save(RUNTIME / 'latest.json', {'at': now().isoformat(), 'results': results})
                    if index + 1 < len(selected):
                        time.sleep(ORDER_PAUSE)
            if apply:
                finish_market(market_orders, results)
        elif selected:
            results = [{'number': proof['name'], 'result': 'waiting_delay'} for proof in selected]
        summary = {'at': now().isoformat(), 'availability': 'inherited_from_import',
                   'queued': len(queue), 'selected': len(selected),
                   'deferred': max(0, len(queue) - MAX_ORDERS), 'results': results,
                   'created': sum(result['result'] in {'created', 'created_resolved'} for result in results),
                   'existing': sum(result['result'] == 'existing' for result in results),
                   'market_ready': sum(result.get('market', {}).get('result') in MARKET_READY for result in results),
                   'market_pending': [result for result in results if 'market' in result
                                      and result['market'].get('result') not in MARKET_READY],
                   'errors': [result for result in results if result['result'] in {'error', 'pending_manual'}
                              or ('market' in result and result['market'].get('result') not in MARKET_READY)]}
        if apply:
            durable_save(RUNTIME / 'latest.json', summary)
        return summary
    finally:
        ms_api.TIMEOUT, ms_api.RETRIES = previous_limits
        os.close(fd)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    summary = run(parser.parse_args().apply)
    print(json.dumps(summary, ensure_ascii=False, default=str))
    raise SystemExit(1 if summary['errors'] else 0)

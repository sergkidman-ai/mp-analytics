# поток: inv
"""Status-only step for customerorders created by the FBS recovery importer.

Runs after the import service. It never creates a demand, changes positions,
or writes to a marketplace. An MS PUT is one-shot with its response as proof.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import requests
import sys
from zoneinfo import ZoneInfo

from core import ms_api_control
from core import ms_api
from ops.order_priority import order_key, is_express
from ops.fbs_ms_import_pilot import save
from ops.fbs_ms_import_read import warehouse_stock
from ops.ms_supplier_priorities import (POLICY_VERSION, LEGACY_POLICY_VERSION,
                                       supplier_priority, base_supplier_priority)
from prices.profiles import STORE_REMOTE
from ops.supplier_shipping_dates import shortage_day, local_created

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/reports/fbs_ms_recovery_20261102/all_import'
RUNTIME = SOURCE / 'status'
HANDLED = RUNTIME / 'handled.json'
LOCK = RUNTIME / 'lock'
MAX_WRITES = 10
WAITING = {'Ожидает подтверждения'}
STATE = {'Подтвержден': '7bc95b1a-f436-11e9-0a80-055c0021dca1',
         'Собирается': '477d431f-32c5-487b-ba8d-c6e452a4a1d4',
         'Неукомплектован': 'd0bef805-f468-11e2-9e2e-7054d21a8d1e'}
Promised = dict[tuple[str, str], Decimal]


class StatusPutUncertain(RuntimeError):
    """The pass must stop after a write whose result is uncertain."""


def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temp = path.with_name(path.name + '.tmp')
    save(temp, data)
    os.replace(temp, path)


def planned_day(status: str, now: datetime, supplier_ids=()) -> str:
    if status == 'Неукомплектован':
        return shortage_day(now, supplier_ids).isoformat()
    msk = now.astimezone(ZoneInfo('Europe/Moscow'))
    if msk < datetime(2026, 11, 5, tzinfo=ZoneInfo('Europe/Moscow')):
        ready_today = (status in {'Подтвержден', 'Собирается'} and
                       msk < datetime(2026, 11, 3, 15, tzinfo=ZoneInfo('Europe/Moscow')))
        day = datetime(2026, 11, 3 if ready_today else 5).date()
    else:
        day = msk.date() + timedelta(days=msk.hour >= 16)
    return day.isoformat()


def card_id(position: dict) -> str:
    assortment = position['assortment']
    return assortment.get('id') or assortment['meta']['href'].split('?', 1)[0].rsplit('/', 1)[-1]


def pos_signature(positions: list[dict]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = defaultdict(Decimal)
    for position in positions:
        result[card_id(position)] += Decimal(str(position['quantity']))
    return dict(result)


def proof_signature(proof: dict) -> dict[str, Decimal]:
    result: dict[str, Decimal] = defaultdict(Decimal)
    for position in proof['payload']['positions']:
        cid = position['assortment']['meta']['href'].rsplit('/', 1)[-1]
        result[cid] += Decimal(str(position['quantity']))
    return dict(result)


def supplier_selection_proof(item: dict) -> bool:
    if type(item.get('purchase_kopeks')) is not int or item['purchase_kopeks'] <= 0:
        return False
    if 'selection_policy' not in item:
        return type(item.get('tier')) is int and item['tier'] in (1, 2, 3)
    policy = item['selection_policy']
    if policy == LEGACY_POLICY_VERSION:
        expected_priority = base_supplier_priority(item.get('supplier_id'))
    elif policy == POLICY_VERSION and item.get('priority_at'):
        try:
            expected_priority = supplier_priority(item.get('supplier_id'), as_of=item['priority_at'])
        except (ValueError, TypeError):
            return False
    else:
        return False
    if (type(item.get('priority')) is not int
            or item['priority'] != expected_priority
            or item.get('stock_store_id') != STORE_REMOTE):
        return False
    try:
        quantity = Decimal(str(item['quantity']))
        available = Decimal(str(item['supplier_available']))
        return quantity.is_finite() and available.is_finite() and quantity > 0 and available >= quantity
    except (KeyError, ValueError, ArithmeticError):
        return False


def provenance(proof: dict) -> tuple[bool, bool]:
    selected = proof['selected']
    manual = any(x.get('manual_fallback') or x.get('manual_parent') for x in selected)
    short = [x for x in selected if not x['available']]
    price_selected = bool(short) and not manual and all(supplier_selection_proof(x) for x in short)
    if price_selected:
        units = defaultdict(Decimal)
        limits = {}
        for item in short:
            if item.get('selection_policy') in (POLICY_VERSION, LEGACY_POLICY_VERSION):
                cid = item.get('card_id')
                units[cid] += Decimal(str(item['quantity']))
                available = Decimal(str(item['supplier_available']))
                limits[cid] = min(limits.get(cid, available), available)
        price_selected = all(quantity <= limits[cid] for cid, quantity in units.items())
    return manual, price_selected


def stock_for(order: dict) -> dict[str, dict]:
    store_id = ms_api.meta_id(order, 'store')
    cards = {card_id(p): p['assortment'] for p in order['positions']['rows']}
    values = list(cards.values())
    found = {}
    for start in range(0, len(values), 20):
        found.update(warehouse_stock(store_id, values[start:start + 20]))
    return found


def all_available(order: dict, stock: dict[str, dict], promised: dict) -> tuple[bool, dict]:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    own_reserves: dict[str, Decimal] = defaultdict(Decimal)
    for position in order['positions']['rows']:
        cid = card_id(position)
        qty = Decimal(str(position['quantity']))
        quantities[cid] += qty
        own_reserves[cid] += Decimal(str(position.get('reserve') or 0))
    evidence = {}
    all_ready = True
    store_id = ms_api.meta_id(order, 'store')
    for cid, qty in quantities.items():
        own = min(qty, own_reserves[cid])
        total = Decimal(str(stock[cid]['stock']))
        reserved = Decimal(str(stock[cid]['reserve']))
        # Legacy dispatch callers still pass a pool keyed only by card id.
        allocated = promised.get((store_id, cid), promised.get(cid, Decimal(0)))
        free_for_order = total - reserved + own - allocated
        evidence[cid] = {'quantity': str(qty), 'own_reserve': str(own),
                         'stock': str(total), 'all_reserve': str(reserved),
                         'free_for_order': str(free_for_order), 'promised': str(allocated)}
        if total < qty or free_for_order < qty:
            all_ready = False
    return all_ready, evidence


def imported_availability(proof: dict) -> tuple[bool | None, dict]:
    """Reuse the importer's availability decision for exactly its chosen lines."""
    selected = proof.get('selected') or []
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    evidence = {}
    store_id = ms_api.meta_id(proof['payload'], 'store')
    try:
        for item in selected:
            cid, qty = item['card_id'], Decimal(str(item['quantity']))
            if not cid or not qty.is_finite() or qty <= 0 or not isinstance(item.get('available'), bool):
                return None, {'reason': 'import_stock_proof_incomplete'}
            if item['available'] and item.get('stock_store_id') not in (None, store_id):
                return None, {'reason': 'import_stock_warehouse_mismatch'}
            quantities[cid] += qty
            snapshot = evidence.setdefault(cid, {'quantity': '0', 'snapshots': []})
            snapshot['quantity'] = str(quantities[cid])
            snapshot['snapshots'].append({k: item.get(k) for k in
                                          ('quantity', 'available', 'stock', 'stock_store_id')})
    except (KeyError, TypeError, InvalidOperation):
        return None, {'reason': 'import_stock_proof_incomplete'}
    if not selected or dict(quantities) != proof_signature(proof):
        return None, {'reason': 'import_positions_proof_mismatch'}
    return all(item['available'] for item in selected), evidence


def desired(order: dict, proof: dict, stock: dict[str, dict],
            promised: dict, now: datetime) -> tuple[str | None, str | None, dict]:
    if order.get('demands'):
        return None, None, {'reason': 'linked_demand_exists'}
    if ms_api.meta_id(order, 'store') != proof['payload']['store']['meta']['href'].rsplit('/', 1)[-1]:
        return None, None, {'reason': 'warehouse_changed'}
    if Decimal(str(order['sum'])) != Decimal(str(proof['total_kopeks'])):
        return None, None, {'reason': 'amount_changed'}
    positions = order.get('positions', {}).get('rows') or []
    if not positions or pos_signature(positions) != proof_signature(proof):
        return None, None, {'reason': 'positions_changed'}
    status = order['state']['name']
    if status not in WAITING:
        return None, None, {'reason': 'already_processed_or_payment_pending'}
    # stock/promised stay in the signature for existing callers; the regular
    # step trusts the importer and never repeats its inventory/reserve reads.
    available, evidence = imported_availability(proof)
    if available is None:
        return None, None, evidence
    manual, priced = provenance(proof)
    if manual:
        return None, None, {'reason': 'manual_product_choice', 'stock': evidence}
    if available:
        target = 'Собирается'
    elif priced:
        target = 'Неукомплектован'
    else:
        return None, None, {'reason': 'shortage_without_price_proof', 'stock': evidence}
    created = local_created(order['created']) if target == 'Неукомплектован' and order.get('created') else now
    supplier_ids = [item.get('supplier_id') for item in proof['selected']]
    day = planned_day(target, created, supplier_ids)
    if target == 'Собирается' and day > now.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat():
        target = 'Подтвержден'
    return target, day, {'reason': 'eligible', 'stock': evidence,
                         'price_selected': priced, 'available': available,
                         'store_id': ms_api.meta_id(order, 'store'), 'source': 'import_preflight'}


def imported_proofs() -> dict[str, dict]:
    proofs = {}
    for folder in SOURCE.glob('next5_*'):
        readback = folder / 'readback.json'
        preflight = folder / 'preflight.json'
        if not readback.exists() or not preflight.exists():
            continue
        number = json.loads(readback.read_text())['name']
        if number in proofs:
            raise RuntimeError('duplicate_import_artifact:' + number)
        proofs[number] = json.loads(preflight.read_text())
    return proofs


def current_orders(numbers: list[str]) -> dict[str, dict]:
    result = {}
    for start in range(0, len(numbers), 12):
        part = numbers[start:start + 12]
        data = ms_api.get('/entity/customerorder', {'limit': 100,
            'filter': ['name=' + n for n in part],
            'expand': 'state,store,positions.assortment'})
        if data.get('meta', {}).get('size') != len(data.get('rows') or []):
            raise RuntimeError('ms_order_list_incomplete')
        for order in data['rows']:
            if order['name'] not in part:
                continue
            if order['name'] in result:
                raise RuntimeError('duplicate_ms_number:' + order['name'])
            result[order['name']] = order
    if len(result) != len(numbers):
        raise RuntimeError('imported_order_missing_from_ms')
    return result


def record_handled(handled: set[str]) -> None:
    atomic_save(HANDLED, {'numbers': sorted(handled)})


def apply_one(order: dict, proof: dict, promised: Promised, now: datetime,
              apply: bool) -> dict:
    number = order['name']
    # Read identity/state/positions only; availability is already proved by import.
    fresh = ms_api.get('/entity/customerorder/' + order['id'],
                       {'expand': 'state,store,positions.assortment'})
    if fresh['name'] != number:
        raise RuntimeError('order_identity_changed')
    if fresh['state']['name'] not in WAITING:
        return {'number': number, 'result': 'unchanged', 'reason': 'already_processed_or_payment_pending'}
    target, day, evidence = desired(fresh, proof, {}, promised, now)
    if target is None:
        return {'number': number, 'result': 'unchanged', **evidence}
    if fresh['state']['name'] == target and str(fresh.get('deliveryPlannedMoment') or '')[:10] == day:
        return {'number': number, 'result': 'already_target', 'state': target, 'planned': day}
    if not apply:
        return {'number': number, 'result': 'would_update', 'from': fresh['state']['name'],
                'to': target, 'planned': day, **evidence}
    folder = RUNTIME / number
    intent = folder / 'intent.json'
    if intent.exists():
        raise RuntimeError('prior_status_intent_requires_reconciliation')
    payload = {'state': {'meta': {'href': ms_api.BASE + '/entity/customerorder/metadata/states/' + STATE[target],
                                  'type': 'state', 'mediaType': 'application/json'}},
               'deliveryPlannedMoment': day + ' 00:00:00'}
    save(intent, {'at': datetime.now(timezone.utc).isoformat(), 'number': number,
                  'ms_id': fresh['id'], 'before_updated': fresh['updated'],
                  'before_state': fresh['state']['name'], 'before_plan': fresh.get('deliveryPlannedMoment'),
                  'decision': evidence, 'payload': payload}, exclusive=True)
    try:
        response = ms_api_control.request("PUT", ms_api.BASE + '/entity/customerorder/' + fresh['id'], sender=requests.put, client="operations-direct", kind="operational",
            params={'expand': 'state,store,positions.assortment'},
            headers={'Authorization': 'Bearer ' + os.environ['MOYSKLAD_TOKEN'],
                     'Content-Type': 'application/json'}, json=payload, timeout=(5, 45))
        body = response.json() if response.ok else response.text[:1000]
        save(folder / 'response.json', {'status': response.status_code,
                                       'body': body})
    except (requests.Timeout, requests.ConnectionError) as exc:
        uncertainty = {'error': type(exc).__name__}
        try:
            actual = ms_api.get('/entity/customerorder/' + fresh['id'], {'expand': 'state'})
            uncertainty.update(actual_state=actual['state']['name'],
                               actual_plan=actual.get('deliveryPlannedMoment'))
        except Exception as check_exc:
            uncertainty['actual_check_error'] = type(check_exc).__name__
        save(folder / 'uncertain.json', uncertainty)
        raise StatusPutUncertain('status_put_uncertain_reconcile_before_retry') from None
    if response.status_code not in (200, 201):
        raise RuntimeError('status_put_rejected')
    after = body
    if (after['id'] != fresh['id'] or after['name'] != number or after['state']['name'] != target or
            str(after.get('deliveryPlannedMoment') or '')[:10] != day or
            ms_api.meta_id(after, 'store') != ms_api.meta_id(fresh, 'store') or
            pos_signature(after['positions']['rows']) != proof_signature(proof) or
            Decimal(str(after['sum'])) != Decimal(str(proof['total_kopeks']))):
        raise RuntimeError('status_readback_mismatch')
    readback = {'name': after['name'], 'id': after['id'], 'state': after['state']['name'],
                'planned': after.get('deliveryPlannedMoment'), 'sum': after['sum'],
                'updated': after['updated'], 'moment': after.get('moment'),
                'created': after.get('created'), 'agent_id': ms_api.meta_id(after,'agent'),
                'is_express': is_express(after) or is_express(proof),
                'positions': [(card_id(p), p['quantity']) for p in after['positions']['rows']]}
    if target == 'Собирается':
        readback.update(shipment_due=True, changed_at=datetime.now(timezone.utc).isoformat(),
                        store_id=ms_api.meta_id(fresh, 'store'))
    atomic_save(folder / 'readback.json', readback)
    return {'number': number, 'result': 'updated', 'from': fresh['state']['name'],
            'to': target, 'planned': day, 'url': after['meta'].get('uuidHref')}


def run(apply: bool) -> dict:
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError('status_step_already_running') from None
    try:
        proofs = imported_proofs()
        handled = set(json.loads(HANDLED.read_text())['numbers']) if HANDLED.exists() else set()
        pending = sorted(set(proofs) - handled)
        orders = current_orders(pending)
        now = datetime.now(timezone.utc)
        promised: Promised = defaultdict(Decimal)
        results = []
        writes = 0
        for number in sorted(pending, key=lambda n: order_key(orders[n],proofs[n])):
            stop_after_error = False
            order, proof = orders[number], proofs[number]
            if order['state']['name'] not in WAITING:
                result = {'number': number, 'result': 'unchanged', 'reason': 'already_processed_or_payment_pending'}
            elif writes >= MAX_WRITES:
                result = {'number': number, 'result': 'deferred_limit'}
            else:
                try:
                    result = apply_one(order, proof, promised, datetime.now(timezone.utc), apply)
                except Exception as exc:
                    result = {'number': number, 'result': 'error',
                              'reason': type(exc).__name__ + ':' + str(exc)[:160]}
                    stop_after_error = isinstance(exc, StatusPutUncertain)
            results.append(result)
            if result['result'] in ('updated', 'already_target'):
                if result['result'] == 'updated':
                    writes += 1
                if apply:
                    handled.add(number)
            elif result['result'] == 'unchanged' and apply:
                handled.add(number)
            if apply:
                record_handled(handled)
                atomic_save(RUNTIME / 'latest.json', {'at': now.isoformat(), 'results': results})
            if result['result'] in ('updated', 'error', 'would_update'):
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if stop_after_error:
                break
        summary = {'scanned': len(pending), 'updated': sum(r['result'] == 'updated' for r in results),
                   'would_update': sum(r['result'] == 'would_update' for r in results),
                   'deferred': sum(r['result'] == 'deferred_limit' for r in results),
                   'errors': [r for r in results if r['result'] == 'error'],
                   'unchanged': sum(r['result'] == 'unchanged' for r in results),
                   'results': results}
        if apply:
            atomic_save(RUNTIME / 'latest.json', {'at': now.isoformat(), **summary})
        return summary
    finally:
        os.close(fd)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    summary = run(args.apply)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('results', 'errors')}, ensure_ascii=False))
    if summary['errors']:
        sys.exit(1)

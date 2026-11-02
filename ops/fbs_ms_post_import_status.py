# поток: inv
"""Status-only step for customerorders created by the FBS recovery importer.

Runs after the import service. It never creates a demand, changes positions,
or writes to a marketplace. An MS PUT is one-shot with intent and readback.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import requests
import sys
from zoneinfo import ZoneInfo

from core import ms_api
from ops.fbs_ms_import_pilot import save
from ops.fbs_ms_import_read import warehouse_stock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/reports/fbs_ms_recovery_20261102/all_import'
RUNTIME = SOURCE / 'status'
HANDLED = RUNTIME / 'handled.json'
LOCK = RUNTIME / 'lock'
MAX_WRITES = 10
WAITING = {'Ожидает подтверждения'}
STATE = {'Подтвержден': '7bc95b1a-f436-11e9-0a80-055c0021dca1',
         'Неукомплектован': 'd0bef805-f468-11e2-9e2e-7054d21a8d1e'}


def atomic_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temp = path.with_name(path.name + '.tmp')
    save(temp, data)
    os.replace(temp, path)


def planned_day(status: str, now: datetime) -> str:
    msk = now.astimezone(ZoneInfo('Europe/Moscow'))
    if msk < datetime(2026, 11, 3, 12, tzinfo=ZoneInfo('Europe/Moscow')):
        day = msk.date() if status == 'Подтвержден' and msk.date() >= datetime(2026, 11, 3).date() else (
            datetime(2026, 11, 3).date() if status == 'Подтвержден' else datetime(2026, 11, 5).date())
    elif msk < datetime(2026, 11, 5, tzinfo=ZoneInfo('Europe/Moscow')):
        day = datetime(2026, 11, 5).date()
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


def provenance(proof: dict) -> tuple[bool, bool]:
    selected = proof['selected']
    manual = any(x.get('manual_fallback') or x.get('manual_parent') for x in selected)
    short = [x for x in selected if not x['available']]
    price_selected = (bool(short) and not manual and all(
        isinstance(x.get('purchase_kopeks'), int) and x['purchase_kopeks'] > 0
        for x in short))
    return manual, price_selected


def stock_for(order: dict) -> dict[str, dict]:
    store_id = ms_api.meta_id(order, 'store')
    cards = {card_id(p): p['assortment'] for p in order['positions']['rows']}
    values = list(cards.values())
    found = {}
    for start in range(0, len(values), 20):
        found.update(warehouse_stock(store_id, values[start:start + 20]))
    return found


def all_available(order: dict, stock: dict[str, dict], promised: dict[str, Decimal]) -> tuple[bool, dict]:
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    own_reserves: dict[str, Decimal] = defaultdict(Decimal)
    for position in order['positions']['rows']:
        cid = card_id(position)
        qty = Decimal(str(position['quantity']))
        quantities[cid] += qty
        own_reserves[cid] += Decimal(str(position.get('reserve') or 0))
    evidence = {}
    all_ready = True
    for cid, qty in quantities.items():
        own = min(qty, own_reserves[cid])
        total = Decimal(str(stock[cid]['stock']))
        reserved = Decimal(str(stock[cid]['reserve']))
        free_for_order = total - reserved + own - promised[cid]
        evidence[cid] = {'quantity': str(qty), 'own_reserve': str(own),
                         'stock': str(total), 'all_reserve': str(reserved),
                         'free_for_order': str(free_for_order)}
        if total < qty or free_for_order < qty:
            all_ready = False
    return all_ready, evidence


def desired(order: dict, proof: dict, stock: dict[str, dict],
            promised: dict[str, Decimal], now: datetime) -> tuple[str | None, str | None, dict]:
    if order.get('demands'):
        return None, None, {'reason': 'linked_demand_exists'}
    if ms_api.meta_id(order, 'store') != proof['payload']['store']['meta']['href'].rsplit('/', 1)[-1]:
        return None, None, {'reason': 'warehouse_changed'}
    if Decimal(str(order['sum'])) != Decimal(str(proof['total_kopeks'])):
        return None, None, {'reason': 'amount_changed'}
    positions = order.get('positions', {}).get('rows') or []
    if not positions or pos_signature(positions) != proof_signature(proof):
        return None, None, {'reason': 'positions_changed'}
    manual, priced = provenance(proof)
    if manual:
        return None, None, {'reason': 'manual_product_choice'}
    status = order['state']['name']
    if status not in WAITING:
        return None, None, {'reason': 'already_processed_or_payment_pending'}
    available, evidence = all_available(order, stock, promised)
    if available:
        target = 'Подтвержден'
    elif priced:
        target = 'Неукомплектован'
    else:
        return None, None, {'reason': 'shortage_without_price_proof', 'stock': evidence}
    day = planned_day(target, now)
    return target, day, {'reason': 'eligible', 'stock': evidence,
                         'price_selected': priced, 'available': available}


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


def apply_one(order: dict, proof: dict, promised: dict[str, Decimal], now: datetime,
              apply: bool) -> dict:
    number = order['name']
    # One fresh order read and one stock read immediately before a possible PUT.
    fresh = ms_api.get('/entity/customerorder/' + order['id'],
                       {'expand': 'state,store,positions.assortment'})
    if fresh['name'] != number:
        raise RuntimeError('order_identity_changed')
    stock = stock_for(fresh)
    target, day, evidence = desired(fresh, proof, stock, promised, now)
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
        response = requests.put(ms_api.BASE + '/entity/customerorder/' + fresh['id'],
            headers={'Authorization': 'Bearer ' + os.environ['MOYSKLAD_TOKEN'],
                     'Content-Type': 'application/json'}, json=payload, timeout=(5, 45))
        save(folder / 'response.json', {'status': response.status_code,
                                       'body': response.json() if response.ok else response.text[:1000]})
    except (requests.Timeout, requests.ConnectionError) as exc:
        actual = ms_api.get('/entity/customerorder/' + fresh['id'], {'expand': 'state'})
        save(folder / 'uncertain.json', {'error': type(exc).__name__,
                                        'actual_state': actual['state']['name'],
                                        'actual_plan': actual.get('deliveryPlannedMoment')})
        raise RuntimeError('status_put_uncertain_reconcile_before_retry') from None
    if response.status_code not in (200, 201):
        raise RuntimeError('status_put_rejected')
    after = ms_api.get('/entity/customerorder/' + fresh['id'],
                       {'expand': 'state,store,positions.assortment'})
    save(folder / 'readback.json', {'name': after['name'], 'id': after['id'],
                                   'state': after['state']['name'],
                                   'planned': after.get('deliveryPlannedMoment'),
                                   'sum': after['sum'], 'updated': after['updated'],
                                   'positions': [(card_id(p), p['quantity']) for p in after['positions']['rows']]})
    if (after['name'] != number or after['state']['name'] != target or
            str(after.get('deliveryPlannedMoment') or '')[:10] != day or
            pos_signature(after['positions']['rows']) != proof_signature(proof) or
            Decimal(str(after['sum'])) != Decimal(str(proof['total_kopeks']))):
        raise RuntimeError('status_readback_mismatch')
    if target == 'Подтвержден':
        for position in fresh['positions']['rows']:
            cid = card_id(position)
            qty = Decimal(str(position['quantity']))
            own = min(qty, Decimal(str(position.get('reserve') or 0)))
            promised[cid] += max(Decimal(0), qty - own)
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
        promised: dict[str, Decimal] = defaultdict(Decimal)
        results = []
        writes = 0
        for number in sorted(pending, key=lambda n: (orders[n]['moment'], n)):
            order, proof = orders[number], proofs[number]
            if order['state']['name'] not in WAITING:
                result = {'number': number, 'result': 'unchanged', 'reason': 'already_processed_or_payment_pending'}
            elif writes >= MAX_WRITES:
                result = {'number': number, 'result': 'deferred_limit'}
            else:
                try:
                    result = apply_one(order, proof, promised, now, apply)
                except Exception as exc:
                    result = {'number': number, 'result': 'error',
                              'reason': type(exc).__name__ + ':' + str(exc)[:160]}
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

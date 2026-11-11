# поток: inv
"""Receipt-triggered fulfillment with cached stock proof and delayed demand stage.

The queue owns debounce and scheduling. This worker never sleeps for the demand
guard: continue/delay_seconds releases the worker for another receipt.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from core import ms_api
from ops.order_priority import order_key
from ops.fbs_ms_import_read import warehouse_stock
from ops.fbs_ms_post_import_shipments import demand_matches, durable_save, rows
from tools import customer_order_batch17_20261022 as batch
from shipment_guard.config import identity

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.private/ms_supply_fulfillment'
IMPORT_SOURCE = ROOT / 'docs/reports/fbs_ms_recovery_20261102/all_import'
ACCEPTED = '5c76771e-dd9b-11e2-e25d-7054d21a8d1e'
INCOMPLETE = 'd0bef805-f468-11e2-9e2e-7054d21a8d1e'
ASSEMBLING = '477d431f-32c5-487b-ba8d-c6e452a4a1d4'
MAX_ORDERS = 10
GUARD_SECONDS = 300
EXPAND = 'state,store,positions.assortment'
_MARKET_INDEX = {}
_MARKET_INDEX_SOURCE = None
_MARKET_INDEX_AT = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def load(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path: Path, value: dict) -> None:
    durable_save(path, value)


def get(path: str, params: dict | None, budget: dict, kind: str) -> dict:
    budget[kind] = budget.get(kind, 0) + 1
    return ms_api.get(path, params)


def amounts(order: dict) -> tuple[Counter, Counter]:
    need, own = Counter(), Counter()
    for position in rows(order):
        cid = ms_api.meta_id(position, 'assortment')
        quantity = Decimal(str(position['quantity']))
        if not cid or quantity <= 0:
            raise ValueError('invalid_order_position')
        need[cid] += quantity
        own[cid] += max(Decimal(0), Decimal(str(position.get('reserve') or 0)))
    return need, Counter({cid: min(own[cid], quantity) for cid, quantity in need.items()})


def code4(position: dict) -> str | None:
    value = str(position.get('assortment', {}).get('externalCode') or '')
    return value if re.fullmatch(r'\d{4}', value) else None


def intersects(order: dict, supply: dict) -> bool:
    product_ids = {ms_api.meta_id(position, 'assortment') for position in rows(supply)}
    codes = {code4(position) for position in rows(supply)} - {None}
    return any(Decimal(str(position.get('reserve') or 0)) > 0
               and (ms_api.meta_id(position, 'assortment') in product_ids or code4(position) in codes)
               for position in rows(order))


def allowed_fbs(order: dict) -> bool:
    return identity(order)[:2] in {('ozon', 'oz_acc1'), ('ozon', 'oz_acc2'),
                                   ('wb', 'wb_acc1'), ('wb', 'wb_acc2'), ('yandex', 'ya_acc1')}


def same_order(current: dict, original: dict, state: str) -> bool:
    return bool(current.get('id') == original['id'] and current.get('name') == original['name']
                and current.get('state', {}).get('name') == state
                and current.get('deliveryPlannedMoment') == original.get('deliveryPlannedMoment')
                and current.get('sum') == original.get('sum')
                and all(ms_api.meta_id(current, key) == ms_api.meta_id(original, key)
                        for key in ('store', 'organization', 'agent'))
                and all(current.get(key) == original.get(key)
                        for key in ('vatSum', 'vatIncluded', 'vatEnabled'))
                and batch.signature(rows(current)) == batch.signature(rows(original)))


def ledger_load() -> dict:
    return load(RUNTIME / 'allocations.json', {'sequence': 0, 'active': {}, 'posted': []})


def availability(order: dict, job: dict, ledger: dict) -> tuple[bool, dict]:
    need, own = amounts(order)
    store = ms_api.meta_id(order, 'store')
    commitments = list(ledger['active'].values()) + [entry for entry in ledger['posted']
                   if entry['sequence'] > job['stock_sequence']]
    evidence, ready = {}, True
    for cid, quantity in need.items():
        stock = job['stock'][cid]
        physical = Decimal(str(stock['stock']))
        reserved = Decimal(str(stock['reserve']))
        committed, promised = Decimal(0), Decimal(0)
        for entry in commitments:
            if entry['store_id'] != store:
                continue
            component = entry['components'].get(cid, {})
            committed += Decimal(str(component.get('quantity', 0)))
            promised += Decimal(str(component.get('unreserved', 0)))
        physical_free = physical - committed
        free = physical - reserved + own[cid] - promised
        evidence[cid] = {'quantity': str(quantity), 'own_reserve': str(own[cid]),
                         'stock': str(physical), 'all_reserve': str(reserved),
                         'physical_uncommitted': str(physical_free), 'free_for_order': str(free),
                         'unreserved': str(quantity - own[cid])}
        ready = ready and physical_free >= quantity and free >= quantity
    return bool(need) and ready, evidence


def candidate_orders(supply: dict, day: str, budget: dict) -> dict:
    store = supply['store']['meta']['href']
    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    result, offset = {}, 0
    while True:
        payload = get('/entity/customerorder', {'limit': 100, 'offset': offset,
            'filter': ['state=' + ms_api.BASE + '/entity/customerorder/metadata/states/' + INCOMPLETE,
                       'deliveryPlannedMoment>=' + day + ' 00:00:00',
                       'deliveryPlannedMoment<' + next_day + ' 00:00:00', 'store=' + store],
            'expand': EXPAND}, budget, 'candidate_lists')
        page = payload.get('rows', [])
        size = payload.get('meta', {}).get('size')
        if not isinstance(size, int) or (size > offset + len(page) and not page):
            raise ValueError('candidate_page_incomplete')
        for order in page:
            if (ms_api.meta_id(order, 'store') != ms_api.meta_id(supply, 'store')
                    or order.get('state', {}).get('name') != 'Неукомплектован'
                    or str(order.get('deliveryPlannedMoment') or '')[:10] != day):
                raise ValueError('candidate_scope_mismatch')
            if allowed_fbs(order) and intersects(order, supply):
                if order['id'] in result:
                    raise ValueError('duplicate_candidate_order')
                result[order['id']] = order
        offset += len(page)
        if offset >= size:
            return result
        if offset >= 10000:
            raise ValueError('candidate_page_budget')


def new_job(receipt_id: str, event_timestamp: str, event_id: str | None, apply: bool) -> tuple[dict, Path | None]:
    event_key = hashlib.sha256((event_id or event_timestamp).encode()).hexdigest()[:24]
    receipt_folder = RUNTIME / receipt_id
    event_path = receipt_folder / 'events' / (event_key + '.json')
    event = load(event_path)
    if event:
        folder = receipt_folder / event['version']
        return load(folder / 'job.json'), folder
    budget = {}
    supply = get('/entity/supply/' + receipt_id, {'expand': EXPAND}, budget, 'receipt')
    if supply.get('id') != receipt_id or not supply.get('updated'):
        raise ValueError('receipt_identity_or_version_missing')
    version = hashlib.sha256((supply['updated'] + '|' + str(ms_api.meta_id(supply, 'state'))
                             + '|' + str(supply.get('applicable'))).encode()).hexdigest()[:24]
    folder = receipt_folder / version
    previous = load(folder / 'job.json')
    if previous:
        previous['budget']['receipt'] = previous['budget'].get('receipt', 0) + 1
        if apply:
            save(folder / 'job.json', previous)
            save(event_path, {'version': version})
        return previous, folder
    job = {'receipt_id': receipt_id, 'version': version, 'supply_updated': supply['updated'],
           'event_timestamp': event_timestamp, 'target_day': now().astimezone(ZoneInfo('Europe/Moscow')).date().isoformat(),
           'budget': budget, 'results': {}, 'order_ids': [], 'candidates': {}, 'stock': {},
           'stock_sequence': ledger_load()['sequence'], 'status': 'active'}
    if ms_api.meta_id(supply, 'state') != ACCEPTED or supply.get('applicable') is not True:
        job.update(status='done', reason='receipt_not_accepted_or_not_applicable')
    else:
        if not rows(supply) or any(Decimal(str(position['quantity'])) <= 0 for position in rows(supply)):
            raise ValueError('receipt_positions_empty_or_invalid')
        if not ms_api.meta_id(supply, 'store'):
            raise ValueError('receipt_warehouse_missing')
        orders = candidate_orders(supply, job['target_day'], budget)
        cards = {}
        for order in orders.values():
            for position in rows(order):
                card = dict(position['assortment'])
                card['id'] = ms_api.meta_id(position, 'assortment')
                cards[card['id']] = card
        values = list(cards.values())
        for start in range(0, len(values), 20):
            job['stock'].update(warehouse_stock(ms_api.meta_id(supply, 'store'), values[start:start + 20],
                get=lambda path, params: get(path, params, budget, 'stock')))
        job['candidates'] = orders
        job['order_ids'] = sorted(orders, key=lambda cid: order_key(orders[cid]))
        if not orders:
            job.update(status='done', reason='no_reserved_matching_orders')
    if apply:
        save(folder / 'supply.json', supply)
        save(folder / 'job.json', job)
        save(event_path, {'version': version})
        save(receipt_folder / 'last_seen.json', {'version': version, 'updated': supply['updated'],
                                                'state_id': ms_api.meta_id(supply, 'state')})
    return job, folder


def checkpoint_path(cid: str) -> Path:
    return RUNTIME / 'orders' / cid / 'checkpoint.json'


def claim(order: dict, job: dict, evidence: dict) -> dict:
    cid = order['id']
    folder = checkpoint_path(cid).parent
    ledger = ledger_load()
    existing = ledger['active'].get(cid)
    owner = job['receipt_id'] + '/' + job['version']
    if existing and existing['owner'] != owner:
        raise ValueError('order_already_claimed')
    save(folder / 'order_before.json', order)
    if not existing:
        ledger['active'][cid] = {'owner': owner, 'store_id': ms_api.meta_id(order, 'store'),
            'components': {card: {'quantity': value['quantity'], 'own_reserve': value['own_reserve'],
                                  'unreserved': value['unreserved']} for card, value in evidence.items()}}
        save(RUNTIME / 'allocations.json', ledger)
    checkpoint = {'owner': owner, 'number': order['name'], 'order_id': cid, 'stage': 'claimed',
                  'original': order, 'stock_proof': evidence}
    save(checkpoint_path(cid), checkpoint)
    return checkpoint


def write_once(folder: Path, label: str, method: str, path: str, body: dict) -> dict:
    guard = folder / (label + '_guard.json')
    if guard.exists() or (folder / (label + '_intent.json')).exists():
        return {'uncertain': True, 'reason': 'previous_intent_no_retry'}
    durable_save(guard, {'at': now().isoformat(), 'method': method, 'path': path, 'body': body}, exclusive=True)
    try:
        response = batch.one_write(folder, label, method, path, body)
    except Exception as exc:
        response = {'uncertain': True, 'exception': type(exc).__name__}
    save(folder / (label + '_durable_response.json'), response)
    return response


def change_state(checkpoint: dict, job: dict) -> dict:
    cid = checkpoint['order_id']
    folder = checkpoint_path(cid).parent
    payload = {'state': {'meta': {'href': ms_api.BASE + '/entity/customerorder/metadata/states/' + ASSEMBLING,
                                  'type': 'state', 'mediaType': 'application/json'}}}
    response = write_once(folder, 'status', 'PUT', '/entity/customerorder/' + cid + '?expand=' + EXPAND, payload)
    after = response.get('body') or {}
    if not (response.get('status') in (200, 201) and same_order(after, checkpoint['original'], 'Собирается')):
        if response.get('uncertain') and not (folder / 'status_resolve.json').exists():
            after = get('/entity/customerorder/' + cid, {'expand': EXPAND}, job['budget'], 'fact_resolves')
            save(folder / 'status_resolve.json', after)
        if not same_order(after, checkpoint['original'], 'Собирается'):
            checkpoint.update(stage='manual', reason='status_not_verified_no_retry')
            save(checkpoint_path(cid), checkpoint)
            return checkpoint
    changed = now()
    checkpoint.update(stage='ms_changed', assembly=after, changed_at=changed.isoformat(),
        shipment_due_at=(changed + timedelta(seconds=0 if after.get('demands') else GUARD_SECONDS)).isoformat())
    save(checkpoint_path(cid), checkpoint)
    return checkpoint


def fresh_orders(orders: list[dict], budget: dict) -> dict:
    payload = get('/entity/customerorder', {'limit': MAX_ORDERS,
        'filter': ['name=' + order['name'] for order in orders], 'expand': EXPAND}, budget, 'order_batches')
    found = payload.get('rows', [])
    if payload.get('meta', {}).get('size', len(found)) != len(found):
        raise ValueError('order_batch_incomplete_or_duplicate')
    result = {}
    wanted = {order['id'] for order in orders}
    for order in found:
        if order['id'] not in wanted or order['id'] in result:
            raise ValueError('order_batch_identity_mismatch')
        result[order['id']] = order
    return result


def demands_for(orders: list[dict], budget: dict) -> dict:
    result, groups = {}, {}
    for order in orders:
        groups.setdefault(order['organization']['meta']['href'], []).append(order['name'])
    for organization, numbers in groups.items():
        payload = get('/entity/demand', {'limit': 20, 'expand': 'positions.assortment',
            'filter': ['organization=' + organization] + ['name=' + number for number in numbers]}, budget, 'demand_batches')
        found = payload.get('rows', [])
        if payload.get('meta', {}).get('size', len(found)) != len(found):
            raise ValueError('demand_batch_incomplete')
        for demand in found:
            if demand.get('name') not in numbers or demand['organization']['meta']['href'] != organization:
                raise ValueError('demand_batch_identity_mismatch')
            result.setdefault((organization, demand['name']), []).append(demand)
    return result


def release_claim(cid: str, demand: dict, original: dict) -> None:
    ledger = ledger_load()
    allocation = ledger['active'].pop(cid, None)
    if allocation:
        original_links = {ms_api.meta_id({'ref': ref}, 'ref') for ref in original.get('demands', [])}
        if demand['id'] not in original_links:
            ledger['sequence'] += 1
            ledger['posted'].append({**allocation, 'order_id': cid, 'demand_id': demand['id'],
                                      'sequence': ledger['sequence']})
        save(RUNTIME / 'allocations.json', ledger)


def ensure_demand(order: dict, checkpoint: dict, existing: list[dict], job: dict) -> dict | None:
    cid = order['id']
    folder = checkpoint_path(cid).parent
    refs = [ms_api.meta_id({'ref': ref}, 'ref') for ref in order.get('demands', [])]
    if len(existing) == 1 and demand_matches(existing[0], order) and (not refs or refs == [existing[0]['id']]):
        save(folder / 'demand_verified.json', existing[0])
        return existing[0]
    if refs or existing:
        checkpoint.update(stage='manual', reason='linked_or_same_number_demand_mismatch')
        save(checkpoint_path(cid), checkpoint)
        return None
    template = ms_api.put('/entity/demand/new', {'customerOrder': {'meta': order['meta']}})
    save(folder / 'template.json', template)
    if (batch.signature(rows(template)) != batch.signature(rows(order))
            or ms_api.meta_id(template, 'customerOrder') != cid
            or any(ms_api.meta_id(template, key) != ms_api.meta_id(order, key) for key in ('store', 'organization', 'agent'))
            # vatSum is calculated by MS and may be absent from /demand/new.
            # The final posted document still must match the original vatSum.
            or any(template.get(key) != order.get(key) for key in ('vatIncluded', 'vatEnabled'))):
        checkpoint.update(stage='manual', reason='demand_template_mismatch')
        save(checkpoint_path(cid), checkpoint)
        return None
    payload = {key: value for key, value in template.items()
               if key not in {'sum', 'payedSum', 'printed', 'published', 'files'}}
    payload.update(positions=rows(template), name=order['name'], applicable=True,
                   moment=now().astimezone(ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S'))
    response = write_once(folder, 'create', 'POST', '/entity/demand?expand=positions.assortment', payload)
    demand = response.get('body') or {}
    if response.get('status') in (200, 201) and demand_matches(demand, order):
        save(folder / 'demand_verified.json', demand)
        return demand
    if (response.get('uncertain') or response.get('status', 0) >= 500) and not (folder / 'create_resolve.json').exists():
        actual = get('/entity/demand', {'limit': 2, 'expand': 'positions.assortment',
            'filter': ['name=' + order['name'], 'organization=' + order['organization']['meta']['href']]},
            job['budget'], 'fact_resolves')
        save(folder / 'create_resolve.json', actual)
        found = actual.get('rows', [])
        if actual.get('meta', {}).get('size', len(found)) == 1 and len(found) == 1 and demand_matches(found[0], order):
            save(folder / 'demand_verified.json', found[0])
            return found[0]
    checkpoint.update(stage='manual', reason='demand_not_verified_no_retry')
    save(checkpoint_path(cid), checkpoint)
    return None


def market_index() -> dict:
    global _MARKET_INDEX, _MARKET_INDEX_SOURCE, _MARKET_INDEX_AT
    if (_MARKET_INDEX_SOURCE == IMPORT_SOURCE and _MARKET_INDEX_AT is not None
            and (now() - _MARKET_INDEX_AT).total_seconds() < 60):
        return _MARKET_INDEX
    index = {}
    for path in sorted(IMPORT_SOURCE.glob('next5_*/preflight.json')):
        market = load(path).get('market', {})
        platform = market.get('platform')
        account = str(market.get('account') or '')
        prefix = {'ozon': 'oz', 'wb': 'wb', 'yandex': 'ya'}.get(platform)
        if prefix and account in {'acc1', 'acc2', '1', '2'}:
            account = prefix + '_acc' + account[-1]
        if prefix and account and market.get('number'):
            index[(platform, account, str(market['number']))] = market
    _MARKET_INDEX, _MARKET_INDEX_SOURCE, _MARKET_INDEX_AT = index, IMPORT_SOURCE, now()
    return index


def market_fulfill(order: dict, folder: Path) -> dict:
    from ops.fbs_market_fulfillment import fulfill
    platform, account, *_ = identity(order)
    market = market_index().get((platform, account, str(order['name'])))
    if market:
        order = {**order, '_market_fulfillment': market}
    return fulfill(order, folder)


def ordered_ids(job: dict, ids=None):
    values=job['order_ids'] if ids is None else ids
    def priority(cid):
        order=job.get('candidates',{}).get(cid)
        if order is None:
            order=(load(checkpoint_path(cid)) or {}).get('original',{'id':cid})
        return order_key(order)
    return sorted(values,key=priority)


def status_stage(job: dict, folder: Path) -> int:
    pending = ordered_ids(job,[cid for cid in job['order_ids'] if cid not in job['results']])[:MAX_ORDERS]
    owner = job['receipt_id'] + '/' + job['version']
    fresh = fresh_orders([job['candidates'][cid] for cid in pending], job['budget']) if job['results'] else job['candidates']
    for cid in pending:
        try:
            checkpoint = load(checkpoint_path(cid))
            active = ledger_load()['active'].get(cid)
            if checkpoint and checkpoint['owner'] != owner or active and active['owner'] != owner:
                job['results'][cid] = {'result': 'owned_by_another_receipt'}
                continue
            if checkpoint and checkpoint['stage'] != 'claimed':
                job['results'][cid] = {'result': checkpoint['stage']}
                continue
            if checkpoint is None and active:
                checkpoint = {'owner': owner, 'number': job['candidates'][cid]['name'], 'order_id': cid,
                    'stage': 'claimed', 'original': load(checkpoint_path(cid).parent / 'order_before.json'),
                    'stock_proof': active['components']}
                save(checkpoint_path(cid), checkpoint)
            if checkpoint and (checkpoint_path(cid).parent / 'status_guard.json').exists():
                checkpoint = change_state(checkpoint, job)
                job['results'][cid] = {'result': checkpoint['stage'], 'reason': checkpoint.get('reason')}
                continue
            order = fresh.get(cid)
            if (not order or not same_order(order, job['candidates'][cid], 'Неукомплектован')
                    or not allowed_fbs(order) or not intersects(order, load(folder / 'supply.json'))):
                job['results'][cid] = {'result': 'unchanged', 'reason': 'order_changed'}
                continue
            if checkpoint is None:
                ready, evidence = availability(order, job, ledger_load())
                if not ready:
                    job['results'][cid] = {'result': 'shortage', 'stock': evidence}
                    continue
                checkpoint = claim(order, job, evidence)
            checkpoint = change_state(checkpoint, job)
            job['results'][cid] = {'result': checkpoint['stage'], 'reason': checkpoint.get('reason')}
        except Exception as exc:
            checkpoint = load(checkpoint_path(cid))
            if checkpoint:
                checkpoint.update(stage='manual', reason=type(exc).__name__)
                save(checkpoint_path(cid), checkpoint)
            job['results'][cid] = {'result': 'manual', 'reason': type(exc).__name__}
        finally:
            save(folder / 'job.json', job)
    return len(pending)


def shipment_stage(job: dict, folder: Path, ids: list[str]) -> int:
    ids=ordered_ids(job,ids)
    originals = [load(checkpoint_path(cid))['original'] for cid in ids]
    fresh = fresh_orders(originals, job['budget'])
    demands = demands_for(list(fresh.values()), job['budget'])
    for cid in ids:
        checkpoint = load(checkpoint_path(cid))
        try:
            order = fresh.get(cid)
            if not order or not same_order(order, checkpoint['original'], 'Собирается'):
                raise ValueError('order_changed_before_demand')
            if checkpoint['stage'] != 'demand_verified':
                key = (order['organization']['meta']['href'], order['name'])
                demand = ensure_demand(order, checkpoint, demands.get(key, []), job)
                if demand is None:
                    job['results'][cid] = {'result': 'manual', 'reason': checkpoint['reason']}
                    continue
                release_claim(cid, demand, checkpoint['original'])
                checkpoint.update(stage='demand_verified', demand_id=demand['id'])
                save(checkpoint_path(cid), checkpoint)
            result = market_fulfill(order, checkpoint_path(cid).parent / 'market')
            checkpoint['market'] = result
            checkpoint['stage'] = 'done' if result.get('result') in {'ready', 'already_ready'} else 'manual'
            checkpoint['reason'] = result.get('reason')
            save(checkpoint_path(cid), checkpoint)
            job['results'][cid] = {'result': checkpoint['stage'], 'demand_id': checkpoint['demand_id'], 'market': result}
        except Exception as exc:
            checkpoint.update(stage='manual', reason=type(exc).__name__)
            save(checkpoint_path(cid), checkpoint)
            job['results'][cid] = {'result': 'manual', 'reason': type(exc).__name__}
        finally:
            save(folder / 'job.json', job)
    return len(ids)


def result_for(job: dict, folder: Path, processed: int) -> dict:
    remaining_status = [cid for cid in job['order_ids'] if cid not in job['results']]
    pending = [cid for cid, result in job['results'].items() if result['result'] in {'ms_changed', 'demand_verified'}]
    delay, not_before = 0, None
    if remaining_status:
        state = 'continue'
    elif pending:
        due = min(datetime.fromisoformat(load(checkpoint_path(cid))['shipment_due_at']) for cid in pending)
        delay = max(0, int((due - now()).total_seconds()) + 1)
        not_before = due.isoformat()
        state = 'continue'
    else:
        state = 'manual' if any(result['result'] == 'manual' for result in job['results'].values()) else 'done'
        job['status'] = state
    summary = {'status': state, 'receipt_id': job['receipt_id'], 'version': job['version'],
               'target_day': job['target_day'], 'processed': processed,
               'remaining': len(remaining_status) + len(pending), 'delay_seconds': delay,
               'not_before': not_before, 'budget': job['budget'], 'results': job['results']}
    save(folder / 'job.json', job)
    save(folder / 'latest.json', summary)
    save(RUNTIME / 'latest.json', summary)
    return summary


def fulfill_receipt(receipt_id: str, event_timestamp: str, *, apply: bool = False,
                    event_id: str | None = None) -> dict:
    receipt_id = str(UUID(receipt_id))
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    fd = os.open(RUNTIME / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
    previous = ms_api.TIMEOUT, ms_api.RETRIES
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ms_api.TIMEOUT, ms_api.RETRIES = 45, 2
        job, folder = new_job(receipt_id, event_timestamp, event_id, apply)
        if not apply:
            ledger = ledger_load()
            preview = {}
            for cid in ordered_ids(job):
                ready, evidence = availability(job['candidates'][cid], job, ledger)
                preview[cid] = {'result': 'would_assemble' if ready else 'shortage', 'stock': evidence}
                if ready:
                    order = job['candidates'][cid]
                    ledger['active'][cid] = {'store_id': ms_api.meta_id(order, 'store'),
                        'components': {card: {'quantity': value['quantity'], 'unreserved': value['unreserved']}
                                       for card, value in evidence.items()}}
            return {'status': 'done', 'preview': True, 'results': preview, 'budget': job['budget'], 'processed': 0, 'remaining': 0}
        if job['status'] in {'done', 'manual'}:
            return result_for(job, folder, 0)
        if any(cid not in job['results'] for cid in job['order_ids']):
            processed = status_stage(job, folder)
        else:
            due = ordered_ids(job,[cid for cid, result in job['results'].items()
                   if result['result'] in {'ms_changed', 'demand_verified'}
                   and datetime.fromisoformat(load(checkpoint_path(cid))['shipment_due_at']) <= now()])[:MAX_ORDERS]
            processed = shipment_stage(job, folder, due) if due else 0
        return result_for(job, folder, processed)
    finally:
        ms_api.TIMEOUT, ms_api.RETRIES = previous
        os.close(fd)

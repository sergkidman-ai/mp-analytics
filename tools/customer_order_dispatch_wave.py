# поток: inv
"""Date-scoped adapter for the existing MS fulfillment and shipment stages.

No product selection, substitution, rescheduling or marketplace shipment occurs.
WB assignment uses only the two supplies explicitly selected for this wave.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import sys
import time

import requests

from core import ms_api
from customer_guard.sources import get_posting
from customer_guard.wb import WBClient
from customer_guard.yandex import YandexClient
from ops.fbs_ms_post_import_status import all_available, stock_for
from shipment_guard.config import identity
from tools import customer_order_batch17_20261022 as b

DAY = '2026-11-03'
SUPPLIES = {'wb_acc1': 'WB-GI-303580630', 'wb_acc2': 'WB-GI-303879593'}
SUPPORTED = {('wb', 'wb_acc1'), ('wb', 'wb_acc2'), ('ozon', 'oz_acc1'),
             ('ozon', 'oz_acc2'), ('yandex', 'ya_acc1')}
EXPAND = {'expand': 'state,store,organization,agent,positions.assortment'}
ROOT = Path('/opt/mp-analytics/.private/ms_order_dispatch_20261103')
SUPPLY_CACHE = {}


def emit(**data):
    print(json.dumps(data, ensure_ascii=False, default=str), flush=True)


def fixed(order, original):
    b.verify_order_unchanged(order, original)
    for key in ('organization', 'agent'):
        if ms_api.meta_id(order, key) != ms_api.meta_id(original, key):
            raise ValueError(key + '_changed')
    for key in ('vatSum', 'vatIncluded', 'vatEnabled', 'deliveryPlannedMoment'):
        if order.get(key) != original.get(key):
            raise ValueError(key + '_changed')
    if order['name'] != original['name'] or not order.get('applicable'):
        raise ValueError('order_identity_or_applicability_changed')


def basic(order):
    if order.get('state', {}).get('name') != 'Подтвержден':
        raise ValueError('already_processed_or_wrong_status')
    if str(order.get('deliveryPlannedMoment', ''))[:10] != DAY:
        raise ValueError('wrong_planned_date')
    if identity(order)[:2] not in SUPPORTED:
        raise ValueError('unsupported_marketplace_account')
    positions = order.get('positions', {})
    rows = positions.get('rows', [])
    if not order.get('applicable') or not order.get('store') or not rows:
        raise ValueError('inactive_order_or_missing_warehouse_or_positions')
    if len(rows) != positions.get('meta', {}).get('size'):
        raise ValueError('incomplete_positions')
    if any(p.get('pack') or p['assortment']['meta']['type'] != 'product'
           or p['assortment'].get('archived') or Decimal(str(p['quantity'])) <= 0
           for p in rows):
        raise ValueError('unsupported_product_position')
    if order.get('demands'):
        raise ValueError('linked_demand_exists')


def wb_get(account, path):
    response = requests.get('https://marketplace-api.wildberries.ru' + path,
        headers={'Authorization': os.environ['WB_TOKEN_' + account[-4:].upper()]},
        timeout=(5, 25))
    response.raise_for_status()
    return response.json()


def supply(account, refresh=False):
    if not refresh and account in SUPPLY_CACHE and time.monotonic()-SUPPLY_CACHE[account][0] < 30:
        return SUPPLY_CACHE[account][1:]
    sid = SUPPLIES[account]
    info = wb_get(account, '/api/v3/supplies/' + sid)
    if info.get('id') != sid or info.get('done') is not False or info.get('closedAt'):
        raise ValueError('wb_supply_not_open')
    data = wb_get(account, '/api/marketplace/v3/supplies/' + sid + '/order-ids')
    ids = data.get('orderIds')
    if not isinstance(ids, list) or len(ids) != len(set(ids)):
        raise ValueError('wb_supply_order_ids_unverified')
    SUPPLY_CACHE[account] = (time.monotonic(), info, set(ids))
    return info, set(ids)


def compatible(info, raw):
    if info.get('cargoType') not in (0, raw.get('cargoType')):
        raise ValueError('wb_supply_cargo_type_mismatch')
    if info.get('crossBorderType') not in (None, raw.get('crossBorderType')):
        raise ValueError('wb_supply_cross_border_mismatch')
    if raw.get('cargoType') != 1 or raw.get('crossBorderType') != 0:
        raise ValueError('wb_order_delivery_type_not_verified')


def market(order, refresh_supply=False):
    platform, account = identity(order)[:2]
    number = order['name']
    if platform == 'ozon':
        obj = get_posting(number, account)
        if obj.get('status') != 'awaiting_packaging':
            raise ValueError('ozon_not_at_assembly_stage:' + str(obj.get('status')))
        return {'platform': platform, 'account': account, 'status': obj['status'],
                'action': 'already_at_assembly_stage_no_transition'}
    if platform == 'yandex':
        rows = YandexClient(budget=1).call({'orderIds': [int(number)]})['orders']
        if len(rows) != 1 or str(rows[0].get('orderId')) != number:
            raise ValueError('market_order_not_found_unambiguously')
        obj = rows[0]
        if (obj.get('programType'), obj.get('fake'), obj.get('status'), obj.get('substatus')) != (
                'FBS', False, 'PROCESSING', 'STARTED'):
            raise ValueError('market_not_at_assembly_stage')
        return {'platform': platform, 'account': account, 'status': 'PROCESSING/STARTED',
                'action': 'already_at_assembly_stage_no_transition'}
    oid = int(number)
    state = WBClient(account=account, budget=1).statuses([oid])[oid]
    info, ids = supply(account, refresh_supply)
    if state.get('wbStatus') != 'waiting' or state.get('supplierStatus') not in ('new', 'confirm'):
        raise ValueError('wb_cancelled_or_advanced:' + str(state))
    if state['supplierStatus'] == 'confirm' and oid not in ids:
        raise ValueError('wb_order_in_another_supply')
    saved = b.read(ROOT / 'wb_new_preflight.json')[account]['new']
    matches = [r for r in saved if r['id'] == oid]
    if oid not in ids:
        if len(matches) != 1:
            raise ValueError('wb_order_identity_or_cargo_unverified')
        compatible(info, matches[0])
    return {'platform': platform, 'account': account, 'status': state,
            'supply_id': SUPPLIES[account], 'included': oid in ids,
            'action': 'already_in_supply' if oid in ids else 'add_to_supply'}


def no_same_number_demand(order):
    found = ms_api.get('/entity/demand', {'limit': 20, 'filter': [
        'name=' + order['name'], 'organization=' + order['organization']['meta']['href']]})
    if found['meta']['size'] != len(found['rows']) or found['rows']:
        raise ValueError('existing_or_ambiguous_same_number_demand')


def stock(order, folder, promised):
    values = stock_for(order)
    ready, evidence = all_available(order, values, promised)
    b.dump(folder / 'stock_proof.json', evidence)
    if not ready:
        raise ValueError('insufficient_current_stock_with_reserves')
    return evidence


def preflight():
    originals = b.read(ROOT / 'scope_originals.json')
    saved = b.read(ROOT / 'preflight_result.json') if (ROOT / 'preflight_result.json').exists() else {}
    results, eligible = saved.get('orders', {}), saved.get('eligible', [])
    promised = defaultdict(lambda: defaultdict(Decimal))
    for cid, order in originals.items():
        folder = ROOT / 'preflight' / cid
        if cid in results:
            if results[cid]['eligible']:
                for pid, item in b.read(folder / 'stock_proof.json').items():
                    promised[ms_api.meta_id(order, 'store')][pid] += max(
                        Decimal(0), Decimal(item['quantity'])-Decimal(item['own_reserve']))
            continue
        try:
            basic(order)
            remote = market(order)
            evidence = stock(order, folder, promised[ms_api.meta_id(order, 'store')])
            for pid, item in evidence.items():
                promised[ms_api.meta_id(order, 'store')][pid] += max(
                    Decimal(0), Decimal(item['quantity']) - Decimal(item['own_reserve']))
            eligible.append(cid)
            results[cid] = {'number': order['name'], 'eligible': True, 'market': remote}
        except Exception as exc:
            results[cid] = {'number': order['name'], 'eligible': False,
                            'reason': type(exc).__name__ + ':' + str(exc)[:200]}
        b.dump(ROOT / 'preflight_result.json', {'eligible': eligible, 'orders': results, 'complete': False})
    b.dump(ROOT / 'preflight_result.json', {'eligible': eligible, 'orders': results, 'complete': True})
    emit(event='preflight', selected=len(eligible), excluded=len(results)-len(eligible),
         exclusions=dict(Counter(v.get('reason') for v in results.values() if not v['eligible'])))


def wb_assign(order, folder, before=None):
    before = market(order) if before is None else before
    if before['platform'] != 'wb' or before['included']:
        b.dump(folder / 'market_final.json', before)
        return before
    intent = folder / 'wb_intent.json'
    if intent.exists():
        raise RuntimeError('prior_wb_intent_requires_reconciliation')
    sid = before['supply_id']
    body = {'orders': [int(order['name'])]}
    b.dump(intent, {'at': b.now().isoformat(), 'account': before['account'],
                    'supply_id': sid, 'body': body})
    try:
        response = requests.patch('https://marketplace-api.wildberries.ru/api/marketplace/v3/supplies/'
            + sid + '/orders', headers={'Authorization': os.environ[
                'WB_TOKEN_' + before['account'][-4:].upper()]}, json=body, timeout=(5, 30))
        b.dump(folder / 'wb_response.json', {'status': response.status_code,
                                             'body': response.text[:2000]})
    except (requests.Timeout, requests.ConnectionError) as exc:
        b.dump(folder / 'wb_response.json', {'uncertain': type(exc).__name__})
    # Never repeat PATCH after a timeout. The membership and status are the proof.
    for attempt in range(3):
        after = market(order, refresh_supply=True)
        if after['included'] and after['status'].get('supplierStatus') == 'confirm':
            break
        time.sleep(2)
    b.dump(folder / 'market_final.json', after)
    if not after['included'] or after['status'].get('supplierStatus') != 'confirm':
        raise RuntimeError('wb_assignment_not_confirmed_stop_order')
    return after


def prepare_batch(ids, index, originals, promised):
    if not 1 <= len(ids) <= 24:
        raise ValueError('dispatch_batch_must_contain_1_to_24_orders')
    folder = ROOT / ('batch_' + str(index))
    if (folder / 'status_result.json').exists():
        raise RuntimeError('batch_already_attempted_inspect_artifacts')
    state = next(s for s in b.read(ROOT / 'metadata.json')['states'] if s['name'] == 'Собирается')
    results, ready = {}, {}
    for cid in ids:
        item = folder / cid
        try:
            current = ms_api.get('/entity/customerorder/' + cid, EXPAND)
            basic(current)
            fixed(current, originals[cid])
            no_same_number_demand(current)
            remote = market(current)
            evidence = stock(current, item, promised[ms_api.meta_id(current, 'store')])
            latest = ms_api.get('/entity/customerorder/' + cid)
            if (latest['updated'] != current['updated'] or latest.get('demands')
                    or ms_api.meta_id(latest, 'state') != ms_api.meta_id(current, 'state')
                    or latest.get('deliveryPlannedMoment') != current.get('deliveryPlannedMoment')):
                raise ValueError('changed_during_preflight')
            b.dump(item / 'order_before.json', current)
            b.dump(item / 'market_before.json', remote)
            response = b.one_write(item, 'status', 'PUT', '/entity/customerorder/' + cid,
                                   {'state': {'meta': state['meta']}})
            after = ms_api.get('/entity/customerorder/' + cid, EXPAND)
            b.dump(item / 'order_after.json', after)
            fixed(after, current)
            if after['state']['name'] != 'Собирается':
                raise RuntimeError('status_write_not_confirmed_no_retry')
            if response.get('status') not in (200, 201):
                b.dump(item / 'status_reconciled.json', {'actual_state': after['state']['name']})
            for pid, proof in evidence.items():
                promised[ms_api.meta_id(current, 'store')][pid] += max(
                    Decimal(0), Decimal(proof['quantity']) - Decimal(proof['own_reserve']))
            ready[cid] = {'verified': True}
            results[cid] = {'number': current['name'], 'result': 'status_verified'}
        except Exception as exc:
            results[cid] = {'number': originals[cid]['name'], 'result': 'held',
                            'reason': type(exc).__name__ + ':' + str(exc)[:200]}
            if (item / 'status_intent.json').exists():
                proof_path = item / 'order_after.json'
                proven_unchanged = False
                if proof_path.exists():
                    actual = b.read(proof_path)
                    proven_unchanged = (actual.get('state', {}).get('name') == 'Подтвержден'
                                        and actual.get('updated') == current.get('updated'))
                if not proven_unchanged:
                    b.dump(folder / 'preparation_result.json', results)
                    raise RuntimeError('partial_status_write_stop_for_reconciliation') from exc
        b.dump(folder / 'preparation_result.json', results)
        emit(stage='status', **results[cid])
        time.sleep(1.3)
    end = b.now()
    b.dump(folder / 'status_result.json', {'orders': ready, 'finished_at': end.isoformat(),
        'shipment_check_not_before': (end + timedelta(minutes=5)).isoformat()})
    return folder


def finish_batch(folder, originals):
    statuses = b.read(folder / 'status_result.json')
    deadline = datetime.fromisoformat(statuses['shipment_check_not_before'])
    while b.now() < deadline:
        time.sleep(min(30, (deadline-b.now()).total_seconds()))
    b.OUT = folder
    original_fixed, original_stock, original_demand = b.verify_order_unchanged, b.stock_check, b.demand_check
    original_write = b.one_write
    def shipment_fixed(order, previous):
        original_fixed(order, previous)
        for key in ('organization', 'agent'):
            if ms_api.meta_id(order, key) != ms_api.meta_id(previous, key):
                raise ValueError(key + '_changed')
        for key in ('vatSum', 'vatIncluded', 'vatEnabled', 'deliveryPlannedMoment'):
            if order.get(key) != previous.get(key):raise ValueError(key + '_changed')
    markets = {}
    def shipment_stock(order, item):
        markets[order['id']] = market(order)
        return original_stock(order, item)
    b.verify_order_unchanged, b.stock_check = shipment_fixed, shipment_stock
    def paced_write(*args, **kwargs):
        time.sleep(1.3)
        return original_write(*args, **kwargs)
    b.one_write = paced_write
    b.demand_check = lambda d,o: (original_demand(d,o) and d['name']==o['name']
        and d.get('vatSum')==o.get('vatSum') and d.get('vatIncluded')==o.get('vatIncluded'))
    try:
        b.shipments()
    finally:
        b.verify_order_unchanged, b.stock_check, b.demand_check = original_fixed, original_stock, original_demand
        b.one_write = original_write
    results = {}
    for cid, entry in b.read(folder / 'shipment_result.json')['orders'].items():
        result = {'number': originals[cid]['name'], 'shipment': entry}
        if entry['verified']:
            try:
                current = b.read(folder / cid / 'shipment' / 'order_before.json')
                fixed(current, originals[cid])
                latest = b.read(folder / cid / 'shipment' / 'order_after.json')
                refs = latest.get('demands', [])
                if (len(refs) != 1 or ms_api.meta_id({'r':refs[0]},'r') != entry['demand_id']):
                    raise ValueError('shipment_links_changed_or_duplicated')
                state = next(s for s in b.read(ROOT / 'metadata.json')['states'] if s['name']=='Собирается')
                if (ms_api.meta_id(latest,'state') != state['id']
                        or latest.get('deliveryPlannedMoment') != originals[cid].get('deliveryPlannedMoment')):
                    raise ValueError('order_advanced_after_shipment')
                previous = markets.get(cid)
                if previous is None and identity(current)[0] != 'wb':
                    previous = b.read(folder / cid / 'market_before.json')
                result['market'] = wb_assign(current, folder / cid, previous)
                result['result'] = 'verified'
            except Exception as exc:
                result.update(result='partial_market', reason=type(exc).__name__+':'+str(exc)[:200])
        else:
            result['result'] = 'partial_ms'
        results[cid] = result
        b.dump(folder / 'final_result.json', results)
        emit(stage='final', **result)
    return results


def resume_wave(originals):
    started = b.read(ROOT / 'apply_started.json')['order_ids']
    processed, folders = set(), []
    for folder in sorted(ROOT.glob('batch_*')):
        proof = folder / 'preparation_result.json'
        if not proof.exists():continue
        prepared = b.read(proof)
        processed.update(prepared)
        if not (folder / 'status_result.json').exists():
            ready = {cid:{'verified':True} for cid,r in prepared.items() if r['result']=='status_verified'}
            not_before = max((datetime.fromtimestamp((folder / cid / 'order_after.json').stat().st_mtime,
                            timezone.utc)+timedelta(minutes=5) for cid in ready), default=b.now())
            b.dump(folder / 'status_result.json', {'orders':ready,
                'shipment_check_not_before':not_before.isoformat(), 'reconstructed_from_verified_readbacks':True})
        if not (folder / 'shipment_result.json').exists():folders.append(folder)
    canaries = [p for p in folders if 'canary' in p.name]
    results = {}
    for folder in canaries:
        proof = finish_batch(folder, originals)
        if not proof or any(r['result']!='verified' for r in proof.values()):
            raise RuntimeError('resumed_canary_not_complete_stop')
        results.update(proof)
    pending = [cid for cid in started if cid not in processed]
    promised = defaultdict(lambda:defaultdict(Decimal))
    # Account for the already prepared orders whose own reserve is not yet full.
    for folder in folders:
        if folder in canaries:continue
        for cid in b.read(folder / 'status_result.json')['orders']:
            for pid, item in b.read(folder / cid / 'stock_proof.json').items():
                promised[ms_api.meta_id(originals[cid],'store')][pid] += max(
                    Decimal(0),Decimal(item['quantity'])-Decimal(item['own_reserve']))
    suffix = datetime.now(timezone.utc).strftime('%H%M%S')
    newer = [prepare_batch(pending[n:n+24], 'resume_'+suffix+'_'+str(n//24), originals, promised)
             for n in range(0,len(pending),24)]
    for folder in [p for p in folders if p not in canaries]+newer:
        results.update(finish_batch(folder, originals))
    # Include any earlier completed folders without repeating their writes.
    for path in ROOT.glob('batch_*/final_result.json'):results.update(b.read(path))
    b.dump(ROOT / 'final_result.json',results)
    emit(event='complete',counts=dict(Counter(r['result'] for r in results.values())))


def run(apply=False, resume=False):
    # Bound slow reads only in this process; other workers retain their settings.
    ms_api.TIMEOUT, ms_api.RETRIES = 45, 2
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not apply:
            return preflight()
        if (ROOT / 'apply_started.json').exists():
            if resume:
                return resume_wave(b.read(ROOT / 'scope_originals.json'))
            raise RuntimeError('wave_already_started_inspect_artifacts')
        originals = b.read(ROOT / 'scope_originals.json')
        pre = b.read(ROOT / 'preflight_result.json')
        # Current user requested a single just-in-time pass instead of repeating
        # the full read-only audit. Previously found shortages remain excluded.
        ids = [cid for cid in originals if cid not in pre['orders'] or pre['orders'][cid]['eligible']]
        b.dump(ROOT / 'apply_started.json', {'at': b.now().isoformat(), 'order_ids': ids,
            'daytime_authorized_by_user': True, 'future_05_11_settings_untouched': True})
        if not ids:
            emit(event='complete', counts={})
            return
        # Prepare the canary first, then overlap its five-minute wait with the
        # remaining status batches. Its shipment is verified before other shipments.
        promised = defaultdict(lambda: defaultdict(Decimal))
        results = {}
        for attempt in range(len(ids)):
            first = next((cid for cid in ids if identity(originals[cid])[0]=='wb'), ids[0])
            ids.remove(first)
            canary = prepare_batch([first], 'canary_' + str(attempt), originals, promised)
            if not b.read(canary / 'status_result.json')['orders']:
                # A staff race without any write is a skip, not a successful test.
                continue
            break
        else:
            emit(event='complete', counts={'skipped_before_write':len(originals)})
            return
        folders = [prepare_batch(ids[n:n+24], n//24+1, originals, promised) for n in range(0,len(ids),24)]
        results = finish_batch(canary, originals)
        if results.get(first, {}).get('result') != 'verified':
            raise RuntimeError('canary_not_complete_stop')
        for folder in folders:results.update(finish_batch(folder, originals))
        b.dump(ROOT / 'final_result.json', results)
        emit(event='complete', counts=dict(Counter(r['result'] for r in results.values())))


if __name__ == '__main__':
    run('--apply' in sys.argv, resume='--resume' in sys.argv)

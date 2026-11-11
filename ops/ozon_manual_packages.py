# поток: inv
"""Wait for human Ozon assembly, then mirror its exact posting partition in MS.

Never calls an Ozon mutation. MS writes have durable, single-attempt intents;
an uncertain write is reconciled by reading its deterministic identity.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import fcntl
import json
from pathlib import Path
import re
from uuid import UUID, uuid5, NAMESPACE_URL

import requests

from core import ms_api, ms_api_control
from core.ozon_posting_lock import account_lock
from core.order_composition_guard import (position_rows, assert_automatic_composition,
                                          ManualCompositionProtected, remember, PROTECTION_DIR)
from shipment_guard.config import identity

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.private/ozon_manual_packages'
PLACES = 'b645e708-2787-11ee-0a80-11480003a2c7'
HOLD = '16ed0c1d-c0ff-11ef-0a80-0ca200297efb'
ASSEMBLING = '477d431f-32c5-487b-ba8d-c6e452a4a1d4'
EXPAND = 'state,store,organization,agent,positions.assortment'
READY = {'awaiting_deliver', 'acceptance_in_progress', 'delivering', 'delivered'}
NUMBER = re.compile(r'\d+-\d+-\d+')


class Deferred(ValueError):
    pass


def save(path, value, exclusive=False):
    from ops.fbs_market_fulfillment import _save
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _save(path, value, exclusive=exclusive)


def load(path):
    return json.loads(path.read_text())


def folder_for(account, number):
    if account not in {'oz_acc1', 'oz_acc2'} or not NUMBER.fullmatch(str(number)):
        raise Deferred('invalid_package_identity')
    return RUNTIME / account / number


def decimal(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise Deferred('invalid_numeric_value') from None
    if not d.is_finite():
        raise Deferred('non_finite_amount')
    return d


def kop(value):
    return int(decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def products(posting):
    result = {}
    for row in posting.get('products', []):
        key = str(row.get('sku', ''))
        if (not key.isdigit() or int(key) <= 0 or key in result
                or type(row.get('quantity')) is not int or row['quantity'] <= 0
                or not row.get('offer_id')):
            raise Deferred('invalid_posting_products')
        result[key] = row
    if not result:
        raise Deferred('empty_posting_products')
    return result


def posting_amount(posting):
    total = Decimal(0)
    for row in products(posting).values():
        price = row.get('price')
        if isinstance(price, dict):
            if price.get('currency') != 'RUB':
                raise Deferred('unsupported_currency')
            price = price.get('amount')
        if row.get('currency_code', 'RUB') != 'RUB' or decimal(price) < 0:
            raise Deferred('invalid_posting_price')
        total += decimal(price) * 100 * row['quantity']
    return kop(total)


def needs_manual(posting):
    """Strict > boundaries; quantity counts marketplace units across all SKU."""
    return (sum(p['quantity'] for p in products(posting).values()) > 8
            or posting_amount(posting) > 3_000_000)


def related(posting):
    value = posting.get('related_postings', {}) or {}
    if not isinstance(value, dict):
        raise Deferred('invalid_related_postings')
    values = value.get('related_posting_numbers', posting.get('related_posting_numbers', [])) or []
    if not isinstance(values, list) or any(not isinstance(v, str) or not NUMBER.fullmatch(v) for v in values):
        raise Deferred('invalid_related_posting_numbers')
    return set(values)


def owns(account, number):
    return bool(number and (folder_for(account, number) / 'registration.json').exists())


def attributes(order, places):
    result = deepcopy(order.get('attributes', []))
    result = [r for r in result if (r.get('id') or r.get('meta', {}).get('href', '').rsplit('/', 1)[-1]) != PLACES]
    result.append({'meta': {'href': ms_api.BASE + '/entity/customerorder/metadata/attributes/' + PLACES,
                           'type': 'attributemetadata', 'mediaType': 'application/json'}, 'value': str(places)})
    return result


def state(uid):
    return {'meta': {'href': ms_api.BASE + '/entity/customerorder/metadata/states/' + uid,
                     'type': 'state', 'mediaType': 'application/json'}}


def signature(rows):
    """Aggregate signature retains manual supplier selection and all sale terms."""
    result = Counter()
    for row in rows:
        key = json.dumps({k: row.get(k) for k in ('price', 'discount', 'vat', 'vatEnabled', 'pack')}, sort_keys=True)
        result[(ms_api.meta_id(row, 'assortment'), key)] += decimal(row['quantity'])
    return result


def amount(rows):
    return sum(kop(decimal(r['quantity']) * decimal(r['price']) *
                   (1 - decimal(r.get('discount', 0)) / 100)) for r in rows)


def clean_rows(rows):
    keys = ('assortment', 'quantity', 'price', 'discount', 'vat', 'vatEnabled', 'pack')
    result = [{k: deepcopy(r[k]) for k in keys if k in r} for r in rows]
    for r in result:
        r['assortment'] = {'meta': deepcopy(r['assortment']['meta'])}
    return result


def version(doc):
    return {'id': doc['id'], 'updated': doc.get('updated'), 'name': doc.get('name'),
            'sum': doc.get('sum'), 'applicable': doc.get('applicable'),
            'state': ms_api.meta_id(doc, 'state'),
            'positions': clean_rows(position_rows(doc['positions']))}


def business_snapshot(order):
    """Only stock counters/links may change as a consequence of our demand write."""
    value = deepcopy(order)
    for key in ('updated', 'reservedSum', 'shippedSum', 'demands'):
        value.pop(key, None)
    for row in position_rows(value['positions']):
        row.pop('reserve', None)
        row.pop('shipped', None)
    return value


def matches(doc, body):
    for key, value in body.items():
        if key == 'positions':
            if signature(position_rows(doc.get('positions'))) != signature(value):
                return False
            if doc.get('sum') != amount(value):
                return False
        elif key in ('organization', 'store', 'agent', 'project', 'customerOrder', 'state', 'contract'):
            if value is not None and ms_api.meta_id(doc, key) != ms_api.meta_id(body, key):
                return False
        elif key == 'attributes':
            expected = next(r['value'] for r in value if r.get('meta', {}).get('href', '').endswith('/' + PLACES))
            actual = [r.get('value') for r in doc.get('attributes', []) if
                      (r.get('id') == PLACES or r.get('meta', {}).get('href', '').endswith('/' + PLACES))]
            if actual != [expected]:
                return False
        elif doc.get(key) != value:
            return False
    return True


class Reconciler:
    def __init__(self, get=None, request=None, market=None):
        self.get = get or ms_api.get
        self.request = request or requests.request
        self.market = market

    def read(self, kind, uid):
        try:
            return self.get('/entity/' + kind + '/' + uid, {'expand': EXPAND})
        except ms_api.MsError as exc:
            if exc.status == 404:
                return None
            raise

    def write(self, folder, label, kind, uid, body, before, lease, validate=None):
        """One network mutation per intent, including PUT. Read before any retry."""
        path = '/entity/' + kind + '/' + uid
        current = self.read(kind, uid)
        intent = folder / (label + '_intent.json')
        if current and matches(current, body):
            if intent.exists():
                saved = load(intent)
                if saved['path'] != path or saved['body'] != body:
                    raise Deferred('write_intent_scope_changed')
            save(folder / (label + '_verified.json'), current)
            return current
        if intent.exists():
            raise Deferred('uncertain_write_no_repeat:' + label)
        if (current is None) != (before is None) or current and version(current) != version(before):
            raise Deferred('ms_document_changed:' + label)
        if validate:
            validate()
        lease.check()
        method = 'PUT' if current else 'POST'
        payload = body if current else dict(body, id=uid)
        save(intent, {'method': method, 'path': path, 'body': body,
                      'before': version(current) if current else None}, exclusive=True)
        try:
            response = ms_api_control.request(method, ms_api.BASE + (path if current else '/entity/' + kind),
                sender=self.request, client='operations-direct', kind='operational',
                headers={'Authorization': 'Bearer ' + ms_api.TOKEN, 'Content-Type': 'application/json'},
                json=payload, timeout=(5, 30), allow_redirects=False)
            save(folder / (label + '_http.json'), {'status': response.status_code})
        except requests.RequestException as exc:
            save(folder / (label + '_transport.json'), {'error': type(exc).__name__})
        actual = self.read(kind, uid)
        if actual is None or not matches(actual, body):
            raise Deferred('write_not_verified:' + label)
        save(folder / (label + '_verified.json'), actual)
        return actual

    def register(self, order, posting, account, lease):
        UUID(order['id'])
        folder = folder_for(account, order['name'])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (folder / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            actual = self.read('customerorder', order['id'])
            if (not actual or actual.get('name') != order['name'] or identity(actual)[:2] != ('ozon', account)
                    or actual.get('state', {}).get('name') not in
                    {'Собирается', 'Подтвержден', 'Не собран в ЛК Озон'}):
                raise Deferred('manual_hold_order_changed')
            if posting.get('posting_number') != order['name'] or posting.get('status') != 'awaiting_packaging':
                raise Deferred('manual_hold_posting_changed')
            registration = folder / 'registration.json'
            if registration.exists():
                original = load(registration)
                if original['order_id'] != order['id']:
                    raise Deferred('manual_hold_identity_collision')
            else:
                save(registration, {'account': account, 'number': order['name'], 'order_id': order['id'],
                                    'posting': posting, 'order': actual}, exclusive=True)
            body = {'state': state(HOLD), 'attributes': attributes(actual, 2)}
            held = self.write(folder, 'hold', 'customerorder', order['id'], body, actual, lease)
            save(folder / 'held.json', held)
            return {'result': 'waiting_operator', 'places': 2}

    def posting(self, account, number, folder, lease):
        from ops.fbs_market_fulfillment import FulfillmentSession
        if self.market is None:
            self.market = FulfillmentSession()
        session = self.market
        return session._ozon_get(folder, {'account': account, 'number': number}, lease,
                                 'posting_' + number)

    def family(self, registration, folder, lease):
        original = registration['posting']
        number = registration['number']
        queue, found = [number], {}
        while queue:
            current = queue.pop(0)
            if current in found:
                continue
            if len(found) >= 100:
                raise Deferred('split_family_size_limit')
            p = self.posting(registration['account'], current, folder, lease)
            if p.get('order_id') != original.get('order_id') or not p.get('order_id'):
                raise Deferred('split_family_order_id_mismatch')
            parent = p.get('parent_posting_number')
            if parent and parent != number:
                raise Deferred('split_family_unexpected_parent')
            found[current] = p
            queue.extend(sorted(related(p) - found.keys()))
        active = [p for p in found.values() if p.get('status') != 'cancelled']
        if not active or any(p.get('status') not in READY for p in active):
            raise Deferred('waiting_operator_assembly')
        expected = Counter({k: r['quantity'] for k, r in products(original).items()})
        actual = Counter()
        for p in active:
            for sku, row in products(p).items():
                before = products(original).get(sku)
                if not before or before['offer_id'] != row['offer_id']:
                    raise Deferred('split_product_identity_changed')
                # Ozon amounts and currencies must survive the operator's partition.
                if posting_amount({'products': [dict(before, quantity=1)]}) != posting_amount({'products': [dict(row, quantity=1)]}):
                    raise Deferred('split_product_price_changed')
                actual[sku] += row['quantity']
        if actual != expected:
            raise Deferred('split_family_quantities_not_conserved')
        if sum(posting_amount(p) for p in active) != posting_amount(original):
            raise Deferred('split_family_amount_not_conserved')
        return sorted(active, key=lambda p: (p['posting_number'] != number, p['posting_number']))

    def lookup(self, kind, name, organization):
        result = self.get('/entity/' + kind, {'limit': 2, 'expand': EXPAND,
            'filter': ['name=' + name, 'organization=' + organization]})
        rows = result.get('rows', [])
        if result.get('meta', {}).get('size') != len(rows) or len(rows) > 1:
            raise Deferred('duplicate_or_incomplete_' + kind)
        if any(r.get('name') != name or r.get('organization', {}).get('meta', {}).get('href') != organization for r in rows):
            raise Deferred('lookup_identity_mismatch')
        return rows[0] if rows else None

    def provenance(self, registration):
        name = registration['number']
        p = ROOT / 'docs/reports/fbs_ms_recovery_20261102/all_import' / ('next5_' + name) / 'preflight.json'
        if p.exists():
            value = load(p)
            m = value.get('market', {})
            if m.get('number') == name and m.get('account', '').removeprefix('oz_') == registration['account'].removeprefix('oz_'):
                return value.get('selected', [])
        return []

    def stock(self, order, folder, label):
        need, own, refs = Counter(), Counter(), {}
        for row in position_rows(order['positions']):
            uid = ms_api.meta_id(row, 'assortment')
            need[uid] += decimal(row['quantity'])
            own[uid] += decimal(row.get('reserve', 0))
            refs[uid] = row['assortment']['meta']['href']
        for uid, quantity in need.items():
            value = self.get('/report/stock/all', {'limit': 20, 'filter': [
                'store=' + order['store']['meta']['href'], 'product=' + refs[uid], 'stockMode=all']})
            save(folder / (label + '_stock_' + uid + '.json'), value)
            rows = [r for r in value.get('rows', []) if r.get('meta', {}).get('href', '').split('?')[0].rsplit('/', 1)[-1] == uid]
            if len(rows) != 1:
                raise Deferred('fresh_stock_not_verified')
            stock, reserve = decimal(rows[0]['stock']), decimal(rows[0]['reserve'])
            if stock < quantity or stock - reserve + min(own[uid], quantity) < quantity:
                raise Deferred('fresh_stock_insufficient')

    def plan(self, registration, family, folder):
        order = self.read('customerorder', registration['order_id'])
        if (order.get('name') != registration['number'] or identity(order)[:2] != ('ozon', registration['account'])
                or ms_api.meta_id(order, 'state') != HOLD):
            raise Deferred('held_order_changed')
        original = registration['posting']
        manual_protected = False
        try:
            assert_automatic_composition(order, self.get)
        except ManualCompositionProtected:
            # The user's explicit Ozon partition rule permits only distributing
            # these exact manual rows between documents, never reselection.
            manual_protected = True
        distribution = partition(order, original, family, self.provenance(registration))
        org = order['organization']['meta']['href']
        demands = order.get('demands', [])
        if len(demands) != 1:
            raise Deferred('partition_requires_one_verified_original_demand')
        demand = self.read('demand', ms_api.meta_id({'d': demands[0]}, 'd'))
        from ops.fbs_ms_post_import_shipments import demand_matches
        if not demand_matches(demand, order):
            raise Deferred('original_demand_mismatch')
        # No financial downstream documents may already reference this shipment.
        if any(doc.get(k) for doc in (order, demand) for k in
               ('salesReturns', 'factureOut', 'invoicesOut', 'payments', 'cashins', 'prepayments')):
            raise Deferred('original_demand_has_downstream_documents')
        children = []
        for i, p in enumerate(family):
            name = p['posting_number']
            existing = self.lookup('customerorder', name, org)
            existing_demand = self.lookup('demand', name, org)
            if existing and existing['id'] != order['id'] or existing_demand and existing_demand['id'] != demand['id']:
                raise Deferred('split_child_already_exists_requires_reconciliation')
            coid = order['id'] if i == 0 else str(uuid5(NAMESPACE_URL, 'mp-analytics/ozon/' + registration['account'] + '/' + name + '/order'))
            did = demand['id'] if i == 0 else str(uuid5(NAMESPACE_URL, 'mp-analytics/ozon/' + registration['account'] + '/' + name + '/demand'))
            rows = distribution[name]
            common = {k: deepcopy(order[k]) for k in ('organization', 'agent', 'store', 'project', 'contract',
                'rate', 'vatEnabled', 'vatIncluded', 'deliveryPlannedMoment', 'description', 'moment') if k in order}
            common.update(name=name, positions=rows)
            body = dict(common, state=state(HOLD), attributes=attributes(order, 1))
            shipment = {k: deepcopy(v) for k, v in common.items() if k not in ('deliveryPlannedMoment', 'description')}
            shipment.update(customerOrder=ms_api.ref('customerorder', coid), applicable=True)
            children.append({'order_id': coid, 'demand_id': did, 'order': body, 'demand': shipment})
        plan = {'registration': registration, 'original_order': order, 'original_demand': demand,
                'manual_protected': manual_protected,
                'family': family, 'children': children}
        save(folder / 'plan.json', plan, exclusive=True)
        return plan

    def apply_plan(self, plan, folder, lease):
        order, demand = plan['original_order'], plan['original_demand']
        # Exact partitions only: manual supplier cards and terms remain invariant in aggregate.
        all_rows = [r for child in plan['children'] for r in child['order']['positions']]
        if signature(all_rows) != signature(position_rows(order['positions'])) or sum(amount(c['order']['positions']) for c in plan['children']) != order['sum']:
            raise Deferred('partition_certificate_invalid')
        steps = [('0000_unpost', 'demand', demand['id'], {'applicable': False})]
        for i, child in enumerate(plan['children']):
            steps += [(f'{i * 2 + 1:04}_order', 'customerorder', child['order_id'], child['order']),
                      (f'{i * 2 + 2:04}_demand', 'demand', child['demand_id'], child['demand'])]
        steps += [(f'{9000 + i:04}_release', 'customerorder', child['order_id'], {'state': state(ASSEMBLING)})
                  for i, child in enumerate(plan['children'])]
        tracked = {}
        for kind, doc in [('customerorder', order), ('demand', demand)]:
            tracked[(kind, doc['id'])] = doc

        def related_order(label, demand):
            coid = ms_api.meta_id(demand, 'customerOrder')
            before = tracked.get(('customerorder', coid))
            if before is None:
                raise Deferred('demand_customer_order_identity_unknown')
            path = folder / ('step_' + label + '_related_order.json')
            actual = load(path) if path.exists() else self.read('customerorder', coid)
            links = {ms_api.meta_id({'d': r}, 'd') for r in actual.get('demands', [])}
            expected = {ms_api.meta_id({'d': r}, 'd') for r in before.get('demands', [])} | {demand['id']}
            if (business_snapshot(actual) != business_snapshot(before) or links != expected):
                raise Deferred('order_changed_during_demand_write')
            if not path.exists():
                save(path, actual)
            tracked[('customerorder', coid)] = actual
        # Completed writes are the only permitted changes to an existing document.
        for label, kind, uid, body in steps:
            p = folder / ('step_' + label + '_verified.json')
            intent = folder / ('step_' + label + '_intent.json')
            if not p.exists() and intent.exists():
                saved = load(intent)
                if saved['path'] != '/entity/' + kind + '/' + uid or saved['body'] != body:
                    raise Deferred('write_intent_scope_changed')
                actual = self.read(kind, uid)
                if actual is None or not matches(actual, body):
                    raise Deferred('uncertain_write_no_repeat:' + label)
                save(p, actual)
            if p.exists():
                doc = load(p)
                if doc['id'] != uid or not matches(doc, body):
                    raise Deferred('partition_checkpoint_mismatch')
                tracked[(kind, uid)] = doc
                if kind == 'demand':
                    related_order(label, doc)

        def validate():
            for (kind, uid), snapshot in tracked.items():
                actual = self.read(kind, uid)
                if actual is None or version(actual) != version(snapshot):
                    raise Deferred('partition_document_changed:' + kind)

        def step(label, kind, uid, body):
            if (folder / ('step_' + label + '_verified.json')).exists():
                return
            before = tracked.get((kind, uid))
            def check():
                validate()
                if kind == 'demand' and body.get('applicable') is True:
                    co = self.read('customerorder', ms_api.meta_id(body, 'customerOrder'))
                    self.stock(co, folder, label)
                    validate()
            result = self.write(folder, 'step_' + label, kind, uid, body, before, lease, check)
            tracked[(kind, uid)] = result
            if kind == 'demand':
                related_order(label, result)

        # Cancel original stock movement first; release excess reservation before
        # creating child orders. Never transiently double the original shipment.
        validate()
        if plan.get('manual_protected'):
            for child in plan['children']:
                if child['order_id'] == order['id'] or (PROTECTION_DIR / (child['order_id'] + '.json')).exists():
                    continue
                remember({'order_id': child['order_id'], 'order_name': child['order']['name'],
                          'reason': 'inherited_manual_composition_ozon_partition',
                          'original_order_id': order['id'], 'partition_plan': str(folder / 'plan.json')})
        for args in steps:
            step(*args)
        for child in plan['children']:
            co = self.read('customerorder', child['order_id'])
            dem = self.read('demand', child['demand_id'])
            from ops.fbs_ms_post_import_shipments import demand_matches
            if not demand_matches(dem, co) or co['name'] != child['order']['name']:
                raise Deferred('partition_final_verification_failed')
        save(folder / 'done.json', {'result': 'synchronized', 'places': len(plan['children']),
                                  'numbers': [c['order']['name'] for c in plan['children']]})
        return load(folder / 'done.json')

    def sync(self, folder, apply=False):
        with (folder / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (folder / 'done.json').exists():
                return load(folder / 'done.json')
            registration = load(folder / 'registration.json')
            with account_lock(registration['account'], timeout=5) as lease:
                if apply and not (folder / 'plan.json').exists():
                    # Also reconcile a crash after registration or the hold PUT.
                    actual = self.read('customerorder', registration['order_id'])
                    body = {'state': state(HOLD), 'attributes': attributes(actual, 2)}
                    if (actual['name'] != registration['number']
                            or identity(actual)[:2] != ('ozon', registration['account'])
                            or actual.get('state', {}).get('name') not in
                            {'Собирается', 'Подтвержден', 'Не собран в ЛК Озон'}):
                        raise Deferred('manual_hold_order_changed')
                    self.write(folder, 'hold', 'customerorder', registration['order_id'], body, actual, lease)
                family = self.family(registration, folder, lease)
                if not apply:
                    return {'result': 'would_synchronize', 'places': len(family)}
                if (folder / 'plan.json').exists():
                    plan = load(folder / 'plan.json')
                    if family_fingerprint(plan['family']) != family_fingerprint(family):
                        raise Deferred('split_family_changed_after_plan')
                else:
                    # A second fresh marketplace check precedes the persisted plan.
                    again = self.family(registration, folder, lease)
                    if family_fingerprint(again) != family_fingerprint(family):
                        raise Deferred('split_family_changed_during_read')
                    plan = self.plan(registration, family, folder)
                return self.apply_plan(plan, folder, lease)


def family_fingerprint(family):
    return sorted((p['posting_number'], p.get('order_id'), p.get('parent_posting_number'),
                   tuple(sorted(related(p))), tuple(sorted((sku, row['offer_id'], row['quantity'],
                   posting_amount({'products': [dict(row, quantity=1)]})) for sku, row in products(p).items()))) for p in family)


def partition(order, original, family, provenance=()):
    """Allocate existing MS rows, including bundle/components, without reselection."""
    market = products(original)
    offers = {r['offer_id']: sku for sku, r in market.items()}
    if len(offers) != len(market):
        raise Deferred('ambiguous_market_offer')
    grouped = defaultdict(list)
    for row in position_rows(order['positions']):
        if (decimal(row['quantity']) <= 0 or decimal(row['quantity']) != decimal(row['quantity']).to_integral_value()
                or decimal(row['price']) < 0 or not 0 <= decimal(row.get('discount', 0)) < 100):
            raise Deferred('invalid_ms_position_quantity_or_price')
        if any(row.get(key) for key in ('things', 'trackingCodes', 'trackingCodes_1162', 'gtd', 'country')):
            raise Deferred('position_tracking_requires_explicit_unit_mapping')
        external = row['assortment'].get('externalCode')
        candidates = {offer for offer in offers if offer[:4] == external}
        if len(offers) == 1:
            candidates = set(offers)
        if len(candidates) != 1:
            candidates = {p['market_code'] for p in provenance if p.get('card_id') == ms_api.meta_id(row, 'assortment') and p.get('market_code') in offers}
        if len(candidates) != 1:
            raise Deferred('ambiguous_ms_to_market_position')
        grouped[next(iter(candidates))].append(deepcopy(row))
    if set(grouped) != set(offers):
        raise Deferred('incomplete_ms_to_market_positions')
    result = {p['posting_number']: [] for p in family}
    for offer, msrows in grouped.items():
        by_component = defaultdict(list)
        for row in msrows:
            by_component[row['assortment'].get('externalCode') or ms_api.meta_id(row, 'assortment')].append(row)
        sku = offers[offer]
        for component_rows in by_component.values():
            total = sum(decimal(r['quantity']) for r in component_rows)
            ratio = total / market[sku]['quantity']
            if ratio <= 0 or ratio != ratio.to_integral_value():
                raise Deferred('component_quantity_not_integral_per_market_unit')
            pool = [[r, decimal(r['quantity'])] for r in component_rows]
            for p in family:
                count = products(p).get(sku, {}).get('quantity', 0)
                left = ratio * count
                for entry in pool:
                    row, remaining = entry
                    take = min(remaining, left)
                    if take:
                        target = clean_rows([row])[0]
                        target['quantity'] = float(take)
                        result[p['posting_number']].append(target)
                        # Mutate this exact pool entry, including duplicate equal rows.
                        remaining -= take
                        left -= take
                        entry[1] = remaining
                if left:
                    raise Deferred('partition_quantity_shortage')
            if any(remaining for _, remaining in pool):
                raise Deferred('partition_quantity_surplus')
    all_rows = [r for rows in result.values() for r in rows]
    if signature(all_rows) != signature(position_rows(order['positions'])):
        raise Deferred('partition_signature_mismatch')
    # Ozon's sale price is the authoritative amount of each actual posting.
    for p in family:
        if amount(result[p['posting_number']]) != posting_amount(p):
            raise Deferred('partition_posting_amount_mismatch')
    if sum(amount(rows) for rows in result.values()) != order['sum']:
        raise Deferred('partition_total_amount_mismatch')
    return result


def run(apply=False):
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    reconciler = Reconciler()
    with (RUNTIME / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        folders = [p.parent for p in RUNTIME.glob('oz_acc*/*/registration.json') if not (p.parent / 'done.json').exists()]
        folders.sort(key=lambda p: (p / 'latest.json').stat().st_mtime if (p / 'latest.json').exists() else 0)
        for folder in folders[:20]:
            try:
                result = reconciler.sync(folder, apply)
            except Exception as exc:
                result = {'result': 'deferred', 'reason': type(exc).__name__ + ':' + str(exc)[:180]}
            if apply:
                save(folder / 'latest.json', result)
            results.append({'number': folder.name, **result})
    return {'apply': apply, 'checked': len(results), 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    print(json.dumps(run(parser.parse_args().apply), ensure_ascii=False))

# поток: inv
"""Marketplace readiness after the caller has confirmed the matching MS shipment.

No MS/supplier/stock writes or reads. ``folder`` is a private, per-order checkpoint.
An unresolved mutation is reconciled once and is never automatically repeated.
The 03.11 WB supplies are explicit scope, never created/closed/delivered here.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import json
import os
import re
import threading
import time

import requests

from collectors.ozon import _headers as ozon_headers
from core.ozon_posting_lock import OzonLockBusy, OzonLockLost, account_lock
from shipment_guard.config import identity
from ops import wb_fbs_supply_policy as wb_supplies

TARGET = 'Готов к отгрузке'
WB_SUPPLIES = {'wb_acc1': 'WB-GI-303580630', 'wb_acc2': 'WB-GI-303879593'}
WB_DAY = '2026-11-03'
GTD_ATTR = 'b5a757d4-85ca-11f1-0a80-184f000fc818'
HOSTS = {'ozon': 'https://api-seller.ozon.ru', 'wb': 'https://marketplace-api.wildberries.ru',
         'yandex': 'https://api.partner.market.yandex.ru'}
READY = {'ready', 'already_ready'}


class Blocked(ValueError):
    pass


class Uncertain(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _save(path, data, *, exclusive=False):
    """Full private evidence, fsync before returning or sending the mutation."""
    path = Path(path)
    temporary = path if exclusive else path.with_name(path.name + '.tmp')
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    if not exclusive:
        os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _load(path):
    return json.loads(Path(path).read_text())


def _posting(data):
    return data.get('result', data)


def _returned(data):
    result = data.get('result')
    if isinstance(result, dict):
        result = result.get('posting_numbers', result.get('postings'))
    if not isinstance(result, list):
        return []
    return [v if isinstance(v, str) else v.get('posting_number') for v in result]


def _wb_b2b(raw):
    """Only explicit flags; missing optional B2B data is left to WB validation."""
    for value in (raw, raw.get('options')):
        if isinstance(value, dict):
            for key in ('isB2b', 'isB2B'):
                if type(value.get(key)) is bool:
                    return value[key]
    return None


def _market_seed(order, platform, account):
    seed = order.get('_market_fulfillment', {})
    if not isinstance(seed, dict):
        return {}
    seed_account = seed.get('account')
    if seed_account in ('acc1', 'acc2'):
        prefix = {'ozon': 'oz', 'wb': 'wb', 'yandex': 'ya'}.get(platform)
        seed_account = prefix + '_' + seed_account if prefix else seed_account
    if seed and (seed.get('platform') != platform or seed_account != account
                 or str(seed.get('number', seed.get('posting_number', ''))) != str(order['name'])):
        raise Blocked('cached_market_identity_mismatch')
    return seed


def _gtds(order, products, required):
    positions = order.get('positions', {})
    rows = positions.get('rows', []) if isinstance(positions, dict) else positions
    if not rows or (isinstance(positions, dict) and positions.get('meta', {}).get('size', len(rows)) != len(rows)):
        raise Blocked('gtd_complete_saved_positions_required')
    sources = []
    for line in rows:
        assortment = line.get('assortment', {})
        values = {str(a['value']) for a in assortment.get('attributes', [])
                  if a.get('value') and (a.get('id') == GTD_ATTR or a.get('name', '').lower() == 'последний гтд')}
        if isinstance(line.get('gtd'), dict) and line['gtd'].get('name'):
            values.add(str(line['gtd']['name']))
        sources.append((str(assortment.get('externalCode', '')), values))
    result, evidence = {}, {}
    for product in products:
        sku = int(product['sku'])
        if sku not in required:
            continue
        matching = [values for code, values in sources if code == str(product['offer_id'])]
        if not matching and len(products) == 1:
            matching = [values for _, values in sources]
        values = set().union(*matching) if matching else set()
        complete = bool(matching) and all(len(value) == 1 for value in matching)
        evidence[str(sku)] = {'offer_id': product['offer_id'], 'candidate_gtds': sorted(values),
                              'complete_component_data': complete, 'source': 'saved_ms_order'}
        if complete and len(values) == 1:
            value = next(iter(values))
            if re.fullmatch(r'\d{8}/\d{6}/\d{7}(?:/\d+)?', value):
                result[sku] = value
    return result, evidence


class FulfillmentSession:
    """Reusable per-batch transport, account rate limits and short-lived WB caches.

    ``_market_fulfillment`` accepts the exact saved import ``preflight.market``.
    Optional ``raw``/``market_order`` and ``market_status`` preserve richer sources.
    """
    def __init__(self, request=None, *, clock=time.monotonic, sleep=time.sleep, cache_ttl=30):
        self.request = request or requests.Session().request
        self.clock, self.sleep, self.cache_ttl = clock, sleep, cache_ttl
        self.next_at, self.supplies, self.wb_new, self.wb_states, self.yandex_sources = {}, {}, {}, {}, {}
        self._mutex = threading.RLock()

    def _headers(self, platform, account):
        if platform == 'ozon':
            return ozon_headers(account)
        if platform == 'yandex':
            return {'Api-Key': os.environ['YANDEX_API_KEY_ACC1']}
        suffix = account[-4:].upper()
        ref = os.getenv('SHIPMENT_GUARD_WB_TOKEN_ENV_' + suffix, 'WB_TOKEN_' + suffix)
        if not re.fullmatch(r'WB_TOKEN_[A-Z_]*' + suffix, ref):
            raise Blocked('wb_token_reference_invalid')
        token = os.getenv('SHIPMENT_GUARD_WB_TOKEN_' + suffix) or os.getenv(ref)
        if not token:
            raise Blocked('wb_token_missing')
        return {'Authorization': token}

    def _call(self, platform, account, folder, label, method, path, *, body=None, params=None, lease=None, mutation=False):
        headers = self._headers(platform, account)
        if mutation:
            intent = folder / ('mut_' + label + '_intent.json')
            try:
                _save(intent, {'at': _now(), 'platform': platform, 'account': account,
                    'method': method, 'path': path, 'body': body}, exclusive=True)
            except FileExistsError:
                raise Uncertain('prior_' + label + '_intent_no_repeat') from None
        delay = self.next_at.get((platform, account), 0) - self.clock()
        if delay > 0:
            self.sleep(delay)
        if lease is not None:
            lease.check()
        self.next_at[(platform, account)] = self.clock() + 1
        try:
            response = self.request(method, HOSTS[platform] + path, headers=headers,
                json=body, params=params, timeout=(5, 30), allow_redirects=False)
        except requests.RequestException as exc:
            _save(folder / (label + '_transport.json'), {'at': _now(), 'error': type(exc).__name__, 'mutation': mutation})
            raise Uncertain(label + '_transport_' + type(exc).__name__) from None
        try:
            data = response.json()
        except ValueError:
            data = {'non_json_body': response.text}
        _save(folder / (label + '_response.json'), {'at': _now(), 'http': response.status_code, 'body': data})
        if response.status_code == 429:
            try:
                wait = max(1, float(response.headers.get('Retry-After', 1)))
            except (TypeError, ValueError):
                wait = 1
            self.next_at[(platform, account)] = self.clock() + wait
        return response.status_code, data

    def _read(self, *args, **kwargs):
        http, data = self._call(*args, **kwargs)
        if http != 200 or not isinstance(data, dict):
            raise Blocked('read_api_http_' + str(http))
        return data

    def _finish(self, folder, base, result, reason, **proof):
        value = dict(base, result=result, reason=reason, **proof)
        value['evidence'] = sorted(str(p) for p in folder.glob('*.json') if p.name != 'result.json')
        _save(folder / 'result.json', value)
        return value

    def fulfill(self, order: dict, folder: Path) -> dict:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        platform, account = identity(order)[:2]
        base = {'platform': platform, 'account': account, 'number': str(order.get('name', '')),
                'order_id': order.get('id'), 'target_status': TARGET}
        with self._mutex, (folder / 'fulfillment.lock').open('a') as lock:
            (folder / 'fulfillment.lock').chmod(0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(base, result='uncertain', reason='another_executor_owns_order', evidence=[])
            marker = folder / 'identity.json'
            if marker.exists() and _load(marker) != base:
                return dict(base, result='blocked', reason='checkpoint_identity_mismatch', evidence=[str(marker)])
            if not marker.exists():
                _save(marker, base, exclusive=True)
            if (folder / 'result.json').exists():
                return _load(folder / 'result.json')
            try:
                if (platform, account) not in {('ozon', 'oz_acc1'), ('ozon', 'oz_acc2'),
                        ('wb', 'wb_acc1'), ('wb', 'wb_acc2'), ('yandex', 'ya_acc1')}:
                    raise Blocked('unsupported_marketplace_account')
                if not re.fullmatch(r'\d+-\d+-\d+' if platform == 'ozon' else r'\d+', base['number']):
                    raise Blocked('invalid_marketplace_order_number')
                seed = _market_seed(order, platform, account)
                context = account_lock(account, timeout=5) if platform == 'ozon' else nullcontext(None)
                with context as lease:
                    try:
                        if list(folder.glob('mut_*_intent.json')):
                            return self._recover(order, folder, base, seed, lease)
                        if platform == 'ozon':
                            return self._ozon(order, folder, base, seed, lease)
                        if platform == 'yandex':
                            return self._yandex(order, folder, base, seed)
                        return self._wb(order, folder, base, seed)
                    except Uncertain:
                        if list(folder.glob('mut_*_intent.json')):
                            return self._recover(order, folder, base, seed, lease)
                        raise
            except Blocked as exc:
                return self._finish(folder, base, 'blocked', str(exc))
            except (Uncertain, requests.RequestException) as exc:
                result = 'uncertain' if list(folder.glob('mut_*_intent.json')) else 'blocked'
                return self._finish(folder, base, result, str(exc) if isinstance(exc, Uncertain) else type(exc).__name__)
            except (OzonLockBusy, OzonLockLost):
                result = 'uncertain' if list(folder.glob('mut_*_intent.json')) else 'blocked'
                return self._finish(folder, base, result, 'ozon_account_lock_unavailable')
            except (KeyError, TypeError, ValueError) as exc:
                return self._finish(folder, base, 'blocked', 'incomplete_marketplace_data_' + type(exc).__name__)

    def _ozon_get(self, folder, base, lease, label='ozon_payload'):
        data = self._read('ozon', base['account'], folder, label, 'POST', '/v3/posting/fbs/get',
                         body={'posting_number': base['number']}, lease=lease)
        posting = _posting(data)
        if posting.get('posting_number') != base['number']:
            raise Blocked('ozon_posting_identity_mismatch')
        return posting

    def _ozon_gtd(self, order, payload, folder, base, lease):
        required = {int(s) for s in payload.get('requirements', {}).get('products_requiring_gtd', [])}
        if not required:
            return
        mapping, evidence = _gtds(order, payload['products'], required)
        selection_path = folder / 'gtd_selection_authorization.json'
        if selection_path.exists():
            selection = _load(selection_path)
            selected, gtd = selection.get('product_ids'), selection.get('gtd')
            if (selection.get('source') != 'explicit_user'
                    or any(selection.get(key) != base[key] for key in ('order_id', 'number', 'account'))
                    or not isinstance(selected, list) or not selected
                    or any(type(sku) is not int for sku in selected)
                    or not set(selected) <= required or not isinstance(gtd, str)
                    or not re.fullmatch(r'\d{8}/\d{6}/\d{7}(?:/\d+)?', gtd)):
                raise Blocked('gtd_selection_authorization_scope_mismatch')
            if any(gtd not in evidence[str(sku)]['candidate_gtds'] for sku in selected):
                raise Blocked('gtd_selection_not_in_saved_source')
            for sku in selected:
                mapping[sku] = gtd
                evidence[str(sku)]['selection_authorization'] = str(selection_path)
        authorization_path = folder / 'gtd_absence_authorization.json'
        if authorization_path.exists():
            authorization = _load(authorization_path)
            selected = authorization.get('product_ids')
            if (authorization.get('source') != 'explicit_user'
                    or any(authorization.get(key) != base[key]
                           for key in ('order_id', 'number', 'account'))
                    or not isinstance(selected, list) or not selected
                    or any(type(sku) is not int for sku in selected)
                    or not set(selected) <= required):
                raise Blocked('gtd_absence_authorization_scope_mismatch')
            if set(selected) & set(mapping):
                raise Blocked('gtd_absence_conflicts_with_real_gtd')
            for sku in selected:
                mapping[sku] = None
                evidence[str(sku)]['absence_authorization'] = str(authorization_path)
        _save(folder / 'gtd_sources.json', {'mapping': mapping, 'evidence': evidence})
        if set(mapping) != required:
            raise Blocked('real_gtd_missing_or_ambiguous_user_selection_required')
        created = self._call('ozon', base['account'], folder, 'exemplar_create', 'POST',
            '/v6/fbs/posting/product/exemplar/create-or-get', body={'posting_number': base['number']}, lease=lease, mutation=True)
        if created[0] != 200:
            if created[0] >= 500 or created[0] < 400:
                raise Uncertain('exemplar_create_response_indeterminate')
            raise Blocked('exemplar_create_http_' + str(created[0]))
        created = _posting(created[1])
        if created.get('posting_number') != base['number']:
            raise Blocked('exemplar_order_identity_mismatch')
        products, expected = [], {}
        for p in created['products']:
            sku = int(p['product_id'])
            if sku in required:
                if len(p['exemplars']) != p['quantity'] or not p['exemplars']:
                    raise Blocked('exemplar_quantity_mismatch')
                expected[sku] = {str(e['exemplar_id']) for e in p['exemplars']}
                products.append({'product_id': sku, 'exemplars': [
                    ({'exemplar_id': e['exemplar_id'], 'is_gtd_absent': True}
                     if mapping[sku] is None else
                     {'exemplar_id': e['exemplar_id'], 'gtd': mapping[sku], 'is_gtd_absent': False})
                    for e in p['exemplars']]})
        if set(expected) != required:
            raise Blocked('exemplar_products_incomplete')
        http, _ = self._call('ozon', base['account'], folder, 'exemplar_set', 'POST',
            '/v6/fbs/posting/product/exemplar/set', body={'posting_number': base['number'], 'products': products}, lease=lease, mutation=True)
        if http != 200:
            if http >= 500 or http < 400:
                raise Uncertain('exemplar_set_response_indeterminate')
            raise Blocked('exemplar_set_http_' + str(http))
        for attempt in range(3):
            data = self._read('ozon', base['account'], folder, 'exemplar_validation_' + str(attempt), 'POST',
                '/v5/fbs/posting/product/exemplar/status', body={'posting_number': base['number']}, lease=lease)
            checked = set()
            for p in _posting(data).get('products', []):
                sku = int(p['product_id'])
                if sku not in expected:
                    continue
                valid = {str(e['exemplar_id']) for e in p.get('exemplars', [])
                         if ((mapping[sku] is None and e.get('is_gtd_absent') is True
                              and e.get('gtd') in (None, ''))
                             or (mapping[sku] is not None and e.get('gtd') == mapping[sku]))
                         and e.get('gtd_check_status') == 'passed' and not e.get('gtd_error_codes')}
                if valid == expected[sku]:
                    checked.add(sku)
            if checked == required:
                return
        raise Blocked('exemplar_gtd_validation_not_confirmed')

    def _ozon(self, order, folder, base, seed, lease):
        payload = seed.get('raw', seed.get('market_order', {}))
        if payload.get('posting_number') != base['number'] or not payload.get('products') or not payload.get('status'):
            payload = self._ozon_get(folder, base, lease)
        if payload['status'] == 'awaiting_deliver':
            return self._finish(folder, base, 'already_ready', 'ozon_awaiting_deliver', status=payload['status'])
        if payload['status'] != 'awaiting_packaging':
            raise Blocked('ozon_status_' + str(payload['status']))
        products = []
        for p in payload['products']:
            if type(p.get('quantity')) is not int or p['quantity'] <= 0 or isinstance(p.get('sku'), bool) or int(p['sku']) <= 0:
                raise Blocked('ozon_invalid_sku_quantity')
            products.append({'product_id': int(p['sku']), 'quantity': p['quantity']})
        if not products:
            raise Blocked('ozon_empty_products')
        self._ozon_gtd(order, payload, folder, base, lease)
        body = {'posting_number': base['number'], 'packages': [{'products': products}], 'with': {'additional_data': True}}
        try:
            http, response = self._call('ozon', base['account'], folder, 'ship', 'POST', '/v4/posting/fbs/ship',
                                         body=body, lease=lease, mutation=True)
        except Uncertain:
            return self._recover(order, folder, base, seed, lease)
        if http == 200 and _returned(response) == [base['number']]:
            return self._finish(folder, base, 'ready', 'ozon_ship_api_confirmed', http=http,
                                status='awaiting_deliver', returned_postings=_returned(response))
        if http >= 500 or http < 400 or 'ALREADY_SHIPPED' in str(response):
            return self._recover(order, folder, base, seed, lease)
        return self._finish(folder, base, 'blocked', 'ozon_ship_rejected', http=http)

    def _yandex_current(self, folder, base, seed, *, refresh=False):
        raw = seed.get('raw', seed.get('market_order', {}))
        campaign = seed.get('campaign_id') or seed.get('campaignId') or raw.get('campaignId')
        status = seed.get('status')
        if isinstance(status, list) and len(status) == 2:
            status, substatus = status
        elif isinstance(status, str) and '/' in status:
            status, substatus = status.split('/', 1)
        else:
            substatus = seed.get('substatus') or raw.get('substatus')
            status = status or raw.get('status')
        if campaign and status and not refresh:
            return {'id': int(base['number']), 'status': status, 'substatus': substatus, 'campaignId': campaign}
        cached = self.yandex_sources.get(base['number'])
        if not refresh and cached and self.clock() - cached[0] < self.cache_ttl:
            return cached[1]
        if campaign:
            data = self._read('yandex', base['account'], folder, 'yandex_current', 'GET',
                f'/v2/campaigns/{int(campaign)}/orders/{base["number"]}')
            current = data.get('order', {})
            if str(current.get('id')) != base['number']:
                raise Blocked('yandex_order_identity_mismatch')
            return dict(current, campaignId=campaign)
        business = os.environ['YANDEX_BUSINESS_ID_ACC1']
        data = self._read('yandex', base['account'], folder, 'yandex_business_binding', 'POST',
            f'/v1/businesses/{business}/orders', body={'orderIds': [int(base['number'])]}, params={'limit': 20})
        rows = [r for r in data.get('orders', []) if str(r.get('orderId')) == base['number']]
        if len(rows) != 1 or not rows[0].get('campaignId') or rows[0].get('fake') is not False or rows[0].get('programType') != 'FBS':
            raise Blocked('yandex_binding_or_fbs_scope_unverified')
        return dict(rows[0], id=rows[0]['orderId'])

    def _yandex(self, order, folder, base, seed):
        current = self._yandex_current(folder, base, seed)
        if (current.get('status'), current.get('substatus')) == ('PROCESSING', 'READY_TO_SHIP'):
            return self._finish(folder, base, 'already_ready', 'yandex_ready_to_ship', status='PROCESSING', substatus='READY_TO_SHIP')
        if (current.get('status'), current.get('substatus')) != ('PROCESSING', 'STARTED'):
            raise Blocked('yandex_not_processing_started')
        _save(folder / 'yandex_binding.json', {'campaign_id': current['campaignId']})
        path = f'/v2/campaigns/{int(current["campaignId"])}/orders/{base["number"]}/status'
        try:
            http, response = self._call('yandex', base['account'], folder, 'status', 'PUT', path,
                body={'order': {'status': 'PROCESSING', 'substatus': 'READY_TO_SHIP'}}, mutation=True)
        except Uncertain:
            return self._recover(order, folder, base, seed, None)
        changed = response.get('order', {})
        if 200 <= http < 300 and str(changed.get('id')) == base['number'] and (changed.get('status'), changed.get('substatus')) == ('PROCESSING', 'READY_TO_SHIP'):
            return self._finish(folder, base, 'ready', 'yandex_status_api_confirmed', http=http, status='PROCESSING', substatus='READY_TO_SHIP')
        if http >= 500 or http < 400:
            return self._recover(order, folder, base, seed, None)
        return self._finish(folder, base, 'blocked', 'yandex_status_rejected', http=http)

    def _wb_state(self, folder, base, *, refresh=False):
        key = (base['account'], int(base['number']))
        cached = self.wb_states.get(key)
        if not refresh and cached and self.clock() - cached[0] < self.cache_ttl:
            return cached[1]
        data = self._read('wb', base['account'], folder, 'wb_order_status', 'POST', '/api/v3/orders/status', body={'orders': [key[1]]})
        rows = data.get('orders', [])
        if len(rows) != 1 or rows[0].get('id') != key[1]:
            raise Blocked('wb_order_identity_mismatch')
        self.wb_states[key] = (self.clock(), rows[0])
        return rows[0]

    def _wb_supply(self, folder, base, *, refresh=False, day=WB_DAY, raw=None, allow_create=False):
        account = base['account']
        key = (account, day)
        cached = self.supplies.get(key)
        if not refresh and cached and self.clock() - cached[0] < self.cache_ttl:
            info, ids = cached[1], cached[2]
        else:
            info = None
            if day == WB_DAY:
                sid = WB_SUPPLIES[account]
                info = self._read('wb', account, folder, 'wb_supply', 'GET', '/api/v3/supplies/' + sid)
                if info.get('id') != sid:
                    raise Blocked('wb_supply_not_open_or_wrong_account')
                if not wb_supplies.opened(info):
                    info = None
            if info is None:
                info = wb_supplies.resolve(self, folder, base, day, raw, _save, allow_create=allow_create)
            sid = info['id']
            data = self._read('wb', account, folder, 'wb_membership', 'GET', '/api/marketplace/v3/supplies/' + sid + '/order-ids')
            values = data.get('orderIds')
            if not isinstance(values, list) or len(values) != len(set(values)):
                raise Blocked('wb_membership_schema')
            ids = set(values)
            self.supplies[key] = (self.clock(), info, ids)
        _save(folder / 'wb_supply_binding.json', {'account': account, 'day': day, 'supply_id': info['id']})
        return info, ids

    def _wb_raw(self, folder, base, seed):
        raw = seed.get('raw', seed.get('market_order', {}))
        if raw.get('id') == int(base['number']) and all(k in raw for k in ('cargoType', 'crossBorderType')):
            return raw
        cached = self.wb_new.get(base['account'])
        if not cached or self.clock() - cached[0] >= self.cache_ttl:
            data = self._read('wb', base['account'], folder, 'wb_new_orders', 'GET', '/api/v3/orders/new')
            rows = data.get('orders')
            if not isinstance(rows, list):
                raise Blocked('wb_new_orders_schema')
            cached = (self.clock(), {r['id']: r for r in rows})
            self.wb_new[base['account']] = cached
        if int(base['number']) not in cached[1]:
            raise Blocked('wb_order_cargo_source_missing')
        return cached[1][int(base['number'])]

    def _wb(self, order, folder, base, seed):
        try:
            day = datetime.fromisoformat(order.get('deliveryPlannedMoment', '')).date().isoformat()
        except (ValueError, TypeError):
            raise Blocked('wb_planned_shipping_day_missing') from None
        state = self._wb_state(folder, base)
        if state.get('wbStatus') != 'waiting' or state.get('supplierStatus') not in ('new', 'confirm'):
            raise Blocked('wb_cancelled_or_advanced')
        raw = self._wb_raw(folder, base, seed) if state['supplierStatus'] == 'new' else None
        if raw and (raw.get('cargoType') != 1 or raw.get('crossBorderType') != 0):
            raise Blocked('wb_order_delivery_compatibility_unverified')
        try:
            with wb_supplies.account_lock(base['account']):
                info, ids = self._wb_supply(folder, base, day=day, raw=raw,
                                            allow_create=state['supplierStatus'] == 'new')
                return self._wb_assign(order, folder, base, seed, state, raw, info, ids)
        except wb_supplies.SupplyError as exc:
            raise Blocked(str(exc)) from None

    def _wb_assign(self, order, folder, base, seed, state, raw, info, ids):
        oid = int(base['number'])
        if oid in ids and state['supplierStatus'] == 'confirm':
            return self._finish(folder, base, 'already_ready', 'wb_confirmed_in_target_supply', supply_id=info['id'])
        if state['supplierStatus'] == 'confirm':
            raise Blocked('wb_order_in_another_supply')
        if info.get('cargoType') not in (0, raw['cargoType']) or info.get('crossBorderType') not in (None, raw['crossBorderType']):
            raise Blocked('wb_supply_cargo_or_crossborder_mismatch')
        supply_b2b, order_b2b = _wb_b2b(info), _wb_b2b(raw)
        if ids and supply_b2b is not None and order_b2b is not None and supply_b2b != order_b2b:
            raise Blocked('wb_supply_b2b_mismatch')
        sid = info['id']
        try:
            http, _ = self._call('wb', base['account'], folder, 'supply_add', 'PATCH',
                '/api/marketplace/v3/supplies/' + sid + '/orders', body={'orders': [oid]}, mutation=True)
        except Uncertain:
            return self._recover(order, folder, base, seed, None)
        if http in (200, 204):
            ids.add(oid)
            info.update(cargoType=raw['cargoType'], crossBorderType=raw['crossBorderType'])
            if order_b2b is not None:
                info['isB2b'] = order_b2b
            wb_supplies.remember(base['account'], info, _save)
            self.wb_states[(base['account'], oid)] = (self.clock(), dict(state, supplierStatus='confirm'))
            return self._finish(folder, base, 'ready', 'wb_supply_add_api_confirmed', http=http, supply_id=sid, supplier_status='confirm')
        if http >= 500:
            return self._recover(order, folder, base, seed, None)
        return self._finish(folder, base, 'blocked', 'wb_supply_add_rejected', http=http, supply_id=sid)

    def _recover(self, order, folder, base, seed, lease):
        """At most one point reconciliation after an unresolved mutation; no retry."""
        for label in ('ship', 'status', 'supply_add'):
            response = folder / (label + '_response.json')
            if not response.exists():
                continue
            saved = _load(response)
            http, body = saved['http'], saved['body']
            if label == 'ship' and http == 200 and _returned(body) == [base['number']]:
                return self._finish(folder, base, 'ready', 'saved_ozon_ship_api_confirmed', http=http, status='awaiting_deliver')
            if label == 'status':
                changed = body.get('order', {})
                if 200 <= http < 300 and str(changed.get('id')) == base['number'] and (changed.get('status'), changed.get('substatus')) == ('PROCESSING', 'READY_TO_SHIP'):
                    return self._finish(folder, base, 'ready', 'saved_yandex_status_api_confirmed', http=http)
            if label == 'supply_add' and http in (200, 204):
                return self._finish(folder, base, 'ready', 'saved_wb_supply_add_api_confirmed', http=http, supply_id=WB_SUPPLIES[base['account']])
        marker = folder / 'reconciliation_started.json'
        if marker.exists():
            return self._finish(folder, base, 'uncertain', 'prior_reconciliation_no_write_repeat')
        _save(marker, {'at': _now()}, exclusive=True)
        try:
            if base['platform'] == 'ozon':
                current = self._ozon_get(folder, base, lease, 'ozon_reconciliation')
                ready = current.get('status') == 'awaiting_deliver'
                proof = {'status': current.get('status')}
            elif base['platform'] == 'yandex':
                fresh = dict(seed)
                binding = folder / 'yandex_binding.json'
                if binding.exists():
                    fresh.update(_load(binding))
                current = self._yandex_current(folder, base, fresh, refresh=True)
                ready = (current.get('status'), current.get('substatus')) == ('PROCESSING', 'READY_TO_SHIP')
                proof = {'status': current.get('status'), 'substatus': current.get('substatus')}
            else:
                state = self._wb_state(folder, base, refresh=True)
                # Membership is the exact known supply, not a new-order/history scan.
                binding_path = folder / 'wb_supply_binding.json'
                if binding_path.exists():
                    binding = _load(binding_path)
                    if binding.get('account') != base['account']:
                        raise Blocked('wb_reconciliation_supply_account_mismatch')
                    sid = binding['supply_id']
                elif str(order.get('deliveryPlannedMoment', ''))[:10] == WB_DAY:
                    sid = WB_SUPPLIES[base['account']]
                else:
                    raise Blocked('wb_reconciliation_supply_binding_missing')
                data = self._read('wb', base['account'], folder, 'wb_reconciliation_membership', 'GET',
                    '/api/marketplace/v3/supplies/' + sid + '/order-ids')
                ready = int(base['number']) in data.get('orderIds', []) and state.get('supplierStatus') == 'confirm' and state.get('wbStatus') == 'waiting'
                proof = {'supply_id': sid, 'supplier_status': state.get('supplierStatus'), 'wb_status': state.get('wbStatus')}
            return self._finish(folder, base, 'ready' if ready else 'uncertain',
                                'point_reconciliation_confirmed' if ready else 'mutation_not_confirmed_no_repeat', **proof)
        except (Blocked, Uncertain, KeyError, TypeError, ValueError):
            return self._finish(folder, base, 'uncertain', 'point_reconciliation_failed_no_repeat')

    def fulfill_many(self, orders, folder: Path):
        """Shared supply/source TTL cache; exact per-order durable checkpoints."""
        orders = list(orders)
        # Status reads accept exact IDs in batches. No history/catalogue scan.
        groups = {}
        for order in orders:
            platform, account = identity(order)[:2]
            if platform == 'wb' and account in WB_SUPPLIES and not (Path(folder) / order['id'] / 'result.json').exists():
                groups.setdefault(account, []).append(int(order['name']))
        for account, ids in groups.items():
            evidence = Path(folder) / ('_wb_batch_' + account)
            evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
            for start in range(0, len(ids), 100):
                wanted = list(dict.fromkeys(ids[start:start+100]))
                try:
                    data = self._read('wb', account, evidence, 'statuses_' + str(start), 'POST',
                                      '/api/v3/orders/status', body={'orders': wanted})
                    rows = data.get('orders', [])
                    if len(rows) != len(wanted) or {r.get('id') for r in rows} != set(wanted):
                        continue
                    for row in rows:
                        self.wb_states[(account, row['id'])] = (self.clock(), row)
                except (Blocked, Uncertain):
                    # Individual exact reads can still proceed; no mutation was sent.
                    pass
        missing_bindings = []
        for order in orders:
            if identity(order)[:2] != ('yandex', 'ya_acc1') or (Path(folder) / order['id'] / 'result.json').exists():
                continue
            try:
                seed = _market_seed(order, 'yandex', 'ya_acc1')
            except Blocked:
                continue
            raw = seed.get('raw', seed.get('market_order', {}))
            if not (seed.get('campaign_id') or seed.get('campaignId') or raw.get('campaignId')):
                missing_bindings.append(int(order['name']))
        if missing_bindings:
            evidence = Path(folder) / '_yandex_batch'
            evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
            business = os.environ['YANDEX_BUSINESS_ID_ACC1']
            for start in range(0, len(missing_bindings), 20):
                wanted = list(dict.fromkeys(missing_bindings[start:start+20]))
                try:
                    data = self._read('yandex', 'ya_acc1', evidence, 'bindings_' + str(start), 'POST',
                        f'/v1/businesses/{business}/orders', body={'orderIds': wanted}, params={'limit': 20})
                    rows = data.get('orders', [])
                    for row in rows:
                        if row.get('orderId') in wanted and row.get('campaignId') and row.get('fake') is False and row.get('programType') == 'FBS':
                            self.yandex_sources[str(row['orderId'])] = (self.clock(), dict(row, id=row['orderId']))
                except (Blocked, Uncertain):
                    pass
        return {o['id']: self.fulfill(o, Path(folder) / o['id']) for o in orders}


_DEFAULT = FulfillmentSession()


def fulfill(order: dict, folder: Path) -> dict:
    return _DEFAULT.fulfill(order, folder)


def fulfill_many(orders, folder: Path) -> dict:
    return _DEFAULT.fulfill_many(orders, folder)

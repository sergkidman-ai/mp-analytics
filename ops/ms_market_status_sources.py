# поток: inv
"""Read-only FBS/FBO status adapters over the existing marketplace clients."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import time
from zoneinfo import ZoneInfo

import requests
from collectors.ozon import _headers
from collectors.ozon_postings import request_page, account_lock
from customer_guard.wb import WBClient
from customer_guard.yandex import YandexClient
from ops.ms_market_status_sync import interpret_wb
from ops.fbs_ms_post_import_shipments import durable_save

MSK = ZoneInfo('Europe/Moscow')
SUPPORTED = {('ozon', 'oz_acc1'), ('ozon', 'oz_acc2'), ('wb', 'wb_acc1'),
             ('wb', 'wb_acc2'), ('yandex', 'ya_acc1')}


def canonical(platform, status):
    if status in ('delivered', 'DELIVERED'):
        return 'delivered'
    if status in ('cancelled', 'CANCELLED'):
        return 'cancelled'
    return 'transit' if status in ('delivering', 'DELIVERY', 'PICKUP') else 'other'


def observation(row, status, scheme, detail, evidence):
    if (row['platform'], row['account']) not in SUPPORTED or scheme not in ('fbs', 'fbo'):
        raise ValueError('unsupported_status_source')
    return {**{key: row[key] for key in ('platform', 'account', 'number')},
            'ms_id': row.get('id'), 'scheme': scheme, 'status': status, 'detail': detail,
            'fetched_at': datetime.now(timezone.utc).isoformat(), 'evidence': str(evidence)}


class Provider:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.wb_statistics = {}

    def save(self, name, value):
        path = self.folder / name
        durable_save(path, value)
        return path

    def ozon(self, row):
        if not re.fullmatch(r'\d+-\d+-\d+', row['number']):
            raise ValueError('invalid_ozon_posting_number')
        schemes = ['fbo', 'fbs'] if row.get('verified_scheme') == 'fbo' else ['fbs', 'fbo']
        errors = []
        with account_lock(row['account'], timeout=5) as lease:
            for scheme in schemes:
                url = '/v2/posting/fbo/get' if scheme == 'fbo' else '/v3/posting/fbs/get'
                try:
                    data = request_page('https://api-seller.ozon.ru' + url, _headers(row['account']),
                        {'posting_number': row['number']}, deadline=time.monotonic() + 45, check_lock=lease.check)
                    post = data.get('result', {})
                    if post.get('posting_number') != row['number'] or not isinstance(post.get('status'), str):
                        raise ValueError('ozon_status_identity_mismatch')
                    path = self.save(row['account'] + '/' + row['number'] + '/' + scheme + '.json', data)
                    return observation(row, canonical('ozon', post['status']), scheme, post['status'], path)
                except Exception as exc:
                    errors.append(type(exc).__name__)
        raise ValueError('ozon_status_unverified_' + '_'.join(errors))

    def yandex(self, rows):
        # Keep the existing credentials, pacing, pagination and response checks.
        # Only this read adapter expands the financial pilot's FBS-only filter.
        def read_both(url, **kwargs):
            kwargs['json'] = dict(kwargs['json'], programTypes=['FBS', 'FBY'])
            return requests.post(url, **kwargs)

        client = YandexClient(request=read_both, budget=max(1, (len(rows) + 19) // 20))
        result = {}
        for start in range(0, len(rows), 20):
            batch = rows[start:start + 20]
            wanted = {int(row['number']): row for row in batch}
            data = client.call({'orderIds': list(wanted)})
            path = self.save('yandex/' + str(start) + '.json', data)
            found = {}
            for post in data['orders']:
                oid = post.get('orderId')
                if oid not in wanted or oid in found:
                    raise ValueError('yandex_status_batch_identity_mismatch')
                found[oid] = post
                if post.get('fake') is not False or post.get('programType') not in ('FBS', 'FBY'):
                    continue
                row = wanted[oid]
                result[row['id']] = observation(row, canonical('yandex', post['status']),
                    'fbo' if post['programType'] == 'FBY' else 'fbs',
                    str(post['status']) + ':' + str(post.get('substatus')), path)
        return result

    def wb_stats(self, account, endpoint):
        key = account, endpoint
        if key in self.wb_statistics:
            return self.wb_statistics[key]
        since = (datetime.now(MSK) - timedelta(days=90)).replace(hour=0, minute=0, second=0, microsecond=0)
        cursor = since.isoformat(timespec='milliseconds').split('+')[0]
        result, seen = [], set()
        for page in range(10):
            response = requests.get('https://statistics-api.wildberries.ru/api/v1/supplier/' + endpoint,
                headers={'Authorization': os.environ['WB_TOKEN_' + account[-4:].upper()]},
                params={'dateFrom': cursor, 'flag': 0}, timeout=(5, 45))
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError('wb_statistics_schema')
            self.save(account + '/fbo/' + endpoint + '_' + str(page) + '.json', rows)
            if not rows:
                self.wb_statistics[key] = result
                return result
            for row in rows:
                identity = (row.get('srid'), row.get('lastChangeDate'), row.get('saleID'))
                if identity not in seen:
                    seen.add(identity)
                    result.append(row)
            following = rows[-1].get('lastChangeDate')
            if not isinstance(following, str) or following <= cursor:
                raise ValueError('wb_statistics_pagination_stalled')
            cursor = following
            time.sleep(60)  # Statistics endpoint: one request/minute, including the final empty page.
        raise ValueError('wb_statistics_page_limit')

    def wb_fbo(self, row):
        orders = self.wb_stats(row['account'], 'orders')
        sales = self.wb_stats(row['account'], 'sales')
        def aliases(item):
            return {str(item[key]) for key in ('srid', 'gNumber', 'odid') if item.get(key) is not None}
        matching = [item for item in orders if row['number'] in aliases(item)
                    and item.get('warehouseType') == 'Склад WB']
        if not matching:
            exact = [item for item in sales if item.get('srid') == row['number']
                     and item.get('warehouseType') == 'Склад WB']
            if (exact and any(str(item.get('saleID', '')).startswith('S') for item in exact)
                    and not any(str(item.get('saleID', '')).startswith('R') for item in exact)):
                path = self.save(row['account'] + '/fbo/sale_' + str(row['id']) + '.json', exact)
                return observation(row, 'delivered', 'fbo', 'WB Statistics:exact_srid_sale', path)
            raise ValueError('wb_fbo_stable_identity_not_found')
        states = set()
        for item in matching:
            srid = item.get('srid')
            if not isinstance(srid, str) or not srid:
                raise ValueError('wb_fbo_srid_missing')
            if any(s.get('srid') == srid and str(s.get('saleID', '')).startswith('R') for s in sales):
                raise ValueError('wb_fbo_return_requires_explicit_status')
            sold = any(s.get('srid') == srid and str(s.get('saleID', '')).startswith('S')
                       and s.get('warehouseType') == 'Склад WB' for s in sales)
            if sold and item.get('isCancel') is True:
                raise ValueError('wb_fbo_cancel_sale_conflict')
            states.add('delivered' if sold else 'cancelled' if item.get('isCancel') is True else 'transit')
        # A basket is terminal only when every assembly task agrees.
        status = next(iter(states)) if len(states) == 1 else 'other'
        path = self.save(row['account'] + '/fbo/match_' + str(row['id']) + '.json',
                         {'matched_orders': matching, 'status': status})
        return observation(row, status, 'fbo', 'WB Statistics:' + status, path)

    def wb(self, rows, account):
        result, normal = {}, []
        fallback = []
        for row in rows:
            if row.get('verified_scheme') == 'fbo' or not row['number'].isdecimal() or int(row['number']) >= 2**63:
                fallback.append(row)
            else:
                normal.append(row)
        client = WBClient(account=account, budget=max(1, (len(normal) + 99) // 100))
        for start in range(0, len(normal), 100):
            batch = normal[start:start + 100]
            wanted = {int(row['number']): row for row in batch}
            data = client.call('/api/v3/orders/status', body={'orders': list(wanted)})
            path = self.save(account + '/statuses_' + str(start) + '.json', data)
            found = set()
            for state in data['orders']:
                oid = state.get('id')
                if oid not in wanted or oid in found:
                    raise ValueError('wb_status_batch_identity_mismatch')
                found.add(oid)
                if state.get('supplierStatus') not in {'new', 'confirm', 'complete', 'cancel', 'cancel_carrier'} or not state.get('wbStatus'):
                    fallback.append(wanted[oid])
                    continue
                status, detail = interpret_wb(state)
                result[wanted[oid]['id']] = observation(wanted[oid], status, 'fbs', detail, path)
            fallback.extend(row for oid, row in wanted.items() if oid not in found)
        for row in fallback:
            try:
                result[row['id']] = self.wb_fbo(row)
            except Exception as exc:
                result[row['id']] = {'error': type(exc).__name__, 'reason': str(exc)[:180]}
        return result

    def fetch(self, rows):
        groups, result = defaultdict(list), {}
        for row in rows:
            if (row['platform'], row['account']) in SUPPORTED:
                groups[row['platform'], row['account']].append(row)
        for (platform, account), group in groups.items():
            try:
                if platform == 'ozon':
                    for row in group:
                        try:
                            result[row['id']] = self.ozon(row)
                        except Exception as exc:
                            result[row['id']] = {'error': type(exc).__name__, 'reason': str(exc)[:180]}
                else:
                    result.update(self.wb(group, account) if platform == 'wb' else self.yandex(group))
            except Exception as exc:
                for row in group:
                    result.setdefault(row['id'], {'error': type(exc).__name__, 'reason': str(exc)[:180]})
        return result

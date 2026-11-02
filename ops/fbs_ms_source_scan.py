# поток: inv
"""Read-only FBS source scan for recovery; no DB or PII persistence."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from ops.fbs_ms_import_pilot import save

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
START = datetime.fromisoformat(os.environ.get('FBS_SCAN_START_UTC',
    '2026-11-01T21:00:00+00:00').replace('Z', '+00:00'))
if START.tzinfo is None:
    raise ValueError('FBS_SCAN_START_UTC_requires_timezone')
START = START.astimezone(timezone.utc)
OUT = ROOT / 'docs/reports/fbs_ms_recovery_20261102/all_import/source_scan.json'


def stamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('source_time_without_zone')
    return dt.astimezone(timezone.utc)


def ozon(account: str, end: datetime) -> list[dict]:
    suffix = account[-4:].upper()
    h = {'Client-Id': os.environ['OZON_CLIENT_ID_' + suffix],
         'Api-Key': os.environ['OZON_API_KEY_' + suffix]}
    cursor = ''; seen = set(); out = []
    for _ in range(100):
        body = {'filter': {'since': START.isoformat().replace('+00:00', 'Z'),
                           'to': end.isoformat().replace('+00:00', 'Z')},
                'limit': 100, 'cursor': cursor, 'with': {'financial_data': True}}
        r = requests.post('https://api-seller.ozon.ru/v4/posting/fbs/list',
                          headers=h, json=body, timeout=(5, 30)); r.raise_for_status()
        d = r.json(); rows = d.get('postings')
        if not isinstance(rows, list) or not isinstance(d.get('has_next'), bool):
            raise RuntimeError('ozon_list_schema')
        for p in rows:
            number = p.get('posting_number'); when = p.get('in_process_at') or p.get('created_at')
            if not number or not when or number in seen:
                raise RuntimeError('ozon_identity_or_duplicate')
            seen.add(number)
            created = stamp(when)
            if created < START or created > end:
                continue
            out.append({'platform': 'ozon', 'account': account, 'number': number,
                        'created_at': created.isoformat(), 'status': p.get('status'),
                        'products': [{'code': x.get('offer_id'), 'quantity': x.get('quantity')}
                                     for x in (p.get('products') or [])]})
        if not d['has_next']:
            return out
        nxt = d.get('cursor')
        if not isinstance(nxt, str) or not nxt or nxt == cursor:
            raise RuntimeError('ozon_cursor_stalled')
        cursor = nxt
    raise RuntimeError('ozon_page_budget')


def wb(account: str, end: datetime) -> list[dict]:
    token = os.environ['WB_TOKEN_' + account[-4:].upper()]
    h = {'Authorization': token}; cursor = 0; orders = {}
    for _ in range(100):
        r = requests.get('https://marketplace-api.wildberries.ru/api/v3/orders',
            headers=h, params={'limit': 20, 'next': cursor,
                               'dateFrom': int(START.timestamp()), 'dateTo': int(end.timestamp())},
            timeout=(5, 25)); r.raise_for_status()
        d = r.json(); rows = d.get('orders')
        if not isinstance(rows, list): raise RuntimeError('wb_list_schema')
        for row in rows:
            if row.get('id') in orders and orders[row['id']] != row:
                raise RuntimeError('wb_duplicate_source')
            orders[row['id']] = row
        if len(rows) < 20: break
        nxt = d.get('next')
        if not isinstance(nxt, int) or nxt <= cursor: raise RuntimeError('wb_cursor_stalled')
        cursor = nxt
    else: raise RuntimeError('wb_page_budget')
    ids = list(orders); states = {}
    for i in range(0, len(ids), 100):
        part = ids[i:i+100]
        r = requests.post('https://marketplace-api.wildberries.ru/api/v3/orders/status',
                          headers=h, json={'orders': part}, timeout=(5, 25)); r.raise_for_status()
        rows = r.json().get('orders') or []
        if len(rows) != len(part) or {x.get('id') for x in rows} != set(part):
            raise RuntimeError('wb_status_partial')
        states.update({x['id']: x for x in rows})
    out = []
    for row in orders.values():
        if row.get('deliveryType') != 'fbs' or not row.get('createdAt'):
            continue
        created = stamp(row['createdAt'])
        if created < START or created > end: continue
        state = states[row['id']]
        out.append({'platform': 'wb', 'account': account, 'number': str(row['id']),
                    'created_at': created.isoformat(),
                    'status': [state.get('supplierStatus'), state.get('wbStatus')],
                    'rid': row.get('rid'), 'article': row.get('article'),
                    'buyer_price_kopeks': row.get('price'), 'currency_code': row.get('currencyCode'),
                    'products': [{'code': row.get('article'), 'quantity': 1}]})
    return out


def yandex(end: datetime) -> list[dict]:
    business = os.environ['YANDEX_BUSINESS_ID_ACC1']
    h = {'Api-Key': os.environ['YANDEX_API_KEY_ACC1']}
    body = {'dates': {'creationDateFrom': START.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat(),
                      'creationDateTo': end.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow')).date().isoformat()},
            'programTypes': ['FBS'], 'fake': False}
    token = None; seen_tokens = set(); seen_orders = {}; out = []
    for _ in range(100):
        params = {'limit': 20}
        if token: params['pageToken'] = token
        r = requests.post(f'https://api.partner.market.yandex.ru/v1/businesses/{business}/orders',
                          headers=h, json=body, params=params, timeout=(5, 25)); r.raise_for_status()
        d = r.json(); rows = d.get('orders')
        if not isinstance(rows, list): raise RuntimeError('yandex_list_schema')
        for row in rows:
            if row.get('programType') != 'FBS' or row.get('fake') is not False:
                continue
            number = str(row.get('orderId')); created = stamp(row['creationDate'])
            key = (row.get('campaignId'), number)
            if key in seen_orders and seen_orders[key] != row:
                raise RuntimeError('yandex_duplicate_source')
            if key in seen_orders: continue
            seen_orders[key] = row
            if created < START or created > end: continue
            out.append({'platform': 'yandex', 'account': 'ya_acc1', 'number': number,
                        'created_at': created.isoformat(),
                        'status': [row.get('status'), row.get('substatus')],
                        'business_id': business, 'campaign_id': row.get('campaignId'),
                        'products': [{'code': x.get('offerId'), 'quantity': x.get('count')}
                                     for x in (row.get('items') or [])]})
        token = (d.get('paging') or {}).get('nextPageToken')
        if not token: return out
        if token in seen_tokens: raise RuntimeError('yandex_cursor_stalled')
        seen_tokens.add(token)
    raise RuntimeError('yandex_page_budget')


def scan():
    end = datetime.now(timezone.utc)
    results = {}; errors = {}
    for account in ('oz_acc1', 'oz_acc2', 'wb_acc1', 'wb_acc2', 'ya_acc1'):
        try:
            rows = yandex(end) if account == 'ya_acc1' else ozon(account, end) if account.startswith('oz_') else wb(account, end)
            results[account] = rows
            print(account, 'rows', len(rows), flush=True)
        except Exception as exc:
            errors[account] = type(exc).__name__ + ':' + str(exc)[:100]
            print(account, 'error', errors[account], flush=True)
    save(OUT, {'window_utc': [START.isoformat(), end.isoformat()],
               'accounts': results, 'errors': errors})
    return {'accounts': {k: len(v) for k, v in results.items()}, 'errors': errors}


if __name__ == '__main__':
    import json
    print(json.dumps(scan(), ensure_ascii=False))

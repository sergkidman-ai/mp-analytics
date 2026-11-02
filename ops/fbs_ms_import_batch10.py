# поток: inv
"""Bounded recovery batch: the ten earliest missing FBS orders from 02.11.

Only explicit --apply writes MS customerorders. No marketplace writes, demands,
schedules or catalogue creation. Every POST is one-shot with an exclusive intent.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import requests

from core import ms_api
from ops.fbs_ms_import_pilot import save
from ops.fbs_ms_import_read import find_customerorder, product_cards, warehouse_stock
from ops.fbs_ms_import_policy import Card, AllocationPool, SelectionError, tier
from ops.fbs_ms_order_payload import Line, STORE, build_payload, line_total
from ops.fbs_ms_import_yandex_pilot import yandex_order
from prices.profiles import STORE_REMOTE

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/fbs_ms_recovery_20261102/batch10'
START = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
# Chronological first ten missing, non-cancelled FBS documents after the outage.
SCOPE = (
    ('wb', 'acc1', '6176186527'), ('wb', 'acc1', '6176190006'),
    ('wb', 'acc1', '6176312951'), ('wb', 'acc1', '6176699856'),
    ('ozon', 'acc1', '93364143-0132-1'), ('ozon', 'acc1', '0165251435-0317-1'),
    ('wb', 'acc1', '6176952570'), ('yandex', 'acc1', '63894926720'),
    ('wb', 'acc1', '6177156618'), ('ozon', 'acc1', '0123848185-0158-1'),
)
assert len(SCOPE) == 10 and len(set(SCOPE)) == 10
# Current Enter evidence from the read-only preflight, in kopeks. Refresh each
# selected document immediately before POST; no card.buyPrice fallback.
RECEIPTS = {
    '2316': ('2316t2', 'easy_print_2026-11-02_p2', 32514),
    '3426': ('3426cs', 'kaktus_msk_2026-10-30_p4', 43800),
    '5845': ('5845wb', 'odissey_2026-10-30_p12', 116980),
    '1169': ('1169oem', 'rapid_2026-11-02_p9', 37459),
}
EXPECTED_DOC_GROUPS = {
    'easy_print': ('2026-11-02', 11), 'odissey': ('2026-10-30', 18),
    'rapid': ('2026-11-02', 15), 'kaktus_msk': ('2026-10-30', 22),
}
STATUSES = {'wb': ('new', 'waiting'), 'yandex': ('PROCESSING', 'STARTED')}


def base_code(market_code: str) -> str:
    """User-confirmed marketplace family rule: first four digits are MS code."""
    if not isinstance(market_code, str) or not re.fullmatch(r'\d{4}.*', market_code):
        raise RuntimeError('market_code_not_four_digit_family')
    return market_code[:4]


def wb_source(number: str) -> dict:
    token = os.environ['WB_TOKEN_ACC1']; headers = {'Authorization': token}
    cursor = 0; found = None
    for _ in range(50):
        response = requests.get('https://marketplace-api.wildberries.ru/api/v3/orders',
            headers=headers, params={'limit': 20, 'next': cursor,
                'dateFrom': int(START.timestamp()),
                'dateTo': int(datetime.now(timezone.utc).timestamp())}, timeout=(5, 25))
        response.raise_for_status(); data = response.json(); rows = data.get('orders')
        if not isinstance(rows, list): raise RuntimeError('wb_order_schema')
        for row in rows:
            if row.get('id') == int(number):
                if found is not None and found != row: raise RuntimeError('wb_duplicate_source')
                found = row
        if len(rows) < 20: break
        nxt = data.get('next')
        if not isinstance(nxt, int) or nxt <= cursor: raise RuntimeError('wb_cursor_stalled')
        cursor = nxt
    else: raise RuntimeError('wb_page_budget')
    if not found or found.get('deliveryType') != 'fbs' or not found.get('article'):
        raise RuntimeError('wb_not_found_or_not_fbs')
    response = requests.post('https://marketplace-api.wildberries.ru/api/v3/orders/status',
        headers=headers, json={'orders': [int(number)]}, timeout=(5, 25))
    response.raise_for_status(); states = response.json().get('orders') or []
    if len(states) != 1 or states[0].get('id') != int(number):
        raise RuntimeError('wb_status_identity')
    current = (states[0].get('supplierStatus'), states[0].get('wbStatus'))
    if current != STATUSES['wb']: raise RuntimeError('wb_not_new_waiting')
    if not found.get('rid') or not found.get('createdAt'):
        raise RuntimeError('wb_identity_incomplete')
    stats = requests.get('https://statistics-api.wildberries.ru/api/v1/supplier/orders',
        headers=headers, params={'dateFrom': '2026-11-02T00:00:00', 'flag': 1}, timeout=(5, 40))
    stats.raise_for_status(); matches = [r for r in stats.json() if r.get('srid') == found['rid']]
    if len(matches) != 1 or matches[0].get('isCancel') is not False:
        raise RuntimeError('wb_full_price_unavailable')
    rub = Decimal(str(matches[0].get('priceWithDisc'))); kop = rub * 100
    if kop <= 0 or not kop.is_finite() or kop != kop.to_integral_value():
        raise RuntimeError('wb_invalid_full_price')
    return {'number': number, 'market_code': found['article'], 'created_at': found['createdAt'],
            'status': current, 'rid': found['rid'], 'lines':
            [{'market_code': found['article'], 'quantity': 1,
              'price_kopeks': int(kop), 'discount_percent': '0'}]}


def ozon_source(number: str) -> dict:
    h = {'Client-Id': os.environ['OZON_CLIENT_ID_ACC1'], 'Api-Key': os.environ['OZON_API_KEY_ACC1']}
    response = requests.post('https://api-seller.ozon.ru/v3/posting/fbs/get', headers=h,
        json={'posting_number': number, 'with': {'financial_data': True}}, timeout=(5, 25))
    response.raise_for_status(); p = response.json().get('result')
    if not isinstance(p, dict) or p.get('posting_number') != number:
        raise RuntimeError('ozon_identity')
    if p.get('status') != 'awaiting_packaging': raise RuntimeError('ozon_not_awaiting_packaging')
    products = p.get('products') or []; finance = (p.get('financial_data') or {}).get('products') or []
    if len(products) != len(finance) or not products:
        raise RuntimeError('ozon_financial_positions_incomplete')
    by_sku = {str(f.get('product_id')): f for f in finance}
    if len(by_sku) != len(finance): raise RuntimeError('ozon_financial_sku_ambiguous')
    lines = []
    for product in products:
        f = by_sku.get(str(product.get('sku')))
        if f is None or not product.get('offer_id') or type(product.get('quantity')) is not int:
            raise RuntimeError('ozon_product_finance_mismatch')
        qty = product['quantity']; old = Decimal(str(f.get('old_price')))
        discount = Decimal(str(f.get('total_discount_percent') or 0))
        if qty <= 0 or old <= 0 or old * 100 != (old * 100).to_integral_value():
            raise RuntimeError('ozon_invalid_quantity_or_price')
        lines.append({'market_code': product['offer_id'], 'quantity': qty,
                      'price_kopeks': int(old * 100), 'discount_percent': str(discount)})
    stamp = p.get('in_process_at') or p.get('created_at')
    if not stamp: raise RuntimeError('ozon_created_at_missing')
    return {'number': number, 'created_at': stamp, 'status': p['status'], 'lines': lines}


def market_source(platform: str, number: str) -> dict:
    if platform == 'wb': return wb_source(number)
    if platform == 'ozon': return ozon_source(number)
    if platform == 'yandex':
        y = yandex_order(number)
        if (y['status'], y['substatus']) != STATUSES['yandex']:
            raise RuntimeError('yandex_not_processing_started')
        return {'number': number, 'created_at': y['created_at'], 'status':
                (y['status'], y['substatus']), 'campaign_id': y['campaign_id'],
                'lines': [{'market_code': y['offer_id'], 'quantity': 1,
                           'price_kopeks': y['price_kopeks'], 'discount_percent': '0'}]}
    raise ValueError('unknown_platform')


def stock_for(cards: list[dict], store_id: str) -> dict:
    result = {}
    for start in range(0, len(cards), 20):
        result.update(warehouse_stock(store_id, cards[start:start + 20]))
    return result


def current_receipt(card_id: str, doc_name: str, expected_price: int) -> dict:
    # Check that the same named source document is still current for its group.
    listing = ms_api.get('/entity/enter', {'limit': 1000, 'filter': [
        'store=' + ms_api.ref('store', STORE_REMOTE)['meta']['href'],
        'moment>=2026-10-30 00:00:00']})
    if listing['meta']['size'] != len(listing['rows']): raise RuntimeError('enter_list_incomplete')
    for key, (day, count) in EXPECTED_DOC_GROUPS.items():
        docs = [x for x in listing['rows'] if re.fullmatch(re.escape(key + '_' + day) + r'_p\d+', x.get('name', ''))]
        newest = max((re.match(re.escape(key) + r'_(\d{4}-\d{2}-\d{2})_p\d+$', x.get('name', '')).group(1)
                      for x in listing['rows'] if re.fullmatch(re.escape(key) + r'_\d{4}-\d{2}-\d{2}_p\d+', x.get('name', ''))), default='')
        if len(docs) != count or newest != day:
            raise RuntimeError('enter_group_changed_since_price_preflight')
    docs = [x for x in listing['rows'] if x.get('name') == doc_name]
    if len(docs) != 1: raise RuntimeError('selected_enter_missing_or_duplicate')
    doc = docs[0]
    page = ms_api.get('/entity/enter/' + doc['id'] + '/positions', {'limit': 1000})
    if page['meta']['size'] != len(page['rows']): raise RuntimeError('enter_positions_incomplete')
    hits = [x for x in page['rows'] if ms_api.meta_id(x, 'assortment') == card_id]
    if len(hits) != 1 or Decimal(str(hits[0].get('price'))) != expected_price:
        raise RuntimeError('selected_purchase_price_changed')
    return {'document': doc_name, 'document_id': doc['id'], 'price_kopeks': expected_price}


def inspect(platform: str, account: str, number: str) -> dict:
    source = market_source(platform, number)
    existing = find_customerorder(number)
    if existing: raise RuntimeError('already_in_ms:' + existing['id'])
    pool = AllocationPool(); chosen = []; total = 0
    for item in source['lines']:
        code = base_code(item['market_code'])
        cards = product_cards(code)
        if not cards: raise RuntimeError('exact_base_product_card_missing:' + code)
        stock = stock_for(cards, STORE[account])
        domain = [Card(c['id'], c['externalCode'], ms_api.meta_id(c, 'supplier') or '',
                       (c.get('code') or '').removeprefix(code).lower(),
                       RECEIPTS.get(code, (None, None, None))[2] if c.get('code') == RECEIPTS.get(code, (None,))[0] else None,
                       Decimal(str(stock[c['id']]['stock'])), Decimal(str(stock[c['id']]['reserve'])),
                       c.get('archived', False)) for c in cards]
        try: selected, available = pool.choose(domain, code, Decimal(item['quantity']))
        except SelectionError as exc: raise RuntimeError('product_selection:' + str(exc)) from None
        matching = next(c for c in cards if c['id'] == selected.id)
        proof = {'market_code': item['market_code'], 'base_code': code,
                 'card_id': selected.id, 'card_code': matching.get('code'),
                 'tier': tier(selected), 'stock': stock[selected.id], 'available': available}
        if not available:
            expected = RECEIPTS.get(code)
            if not expected or matching.get('code') != expected[0] or selected.purchase_kopeks != expected[2]:
                raise RuntimeError('purchase_selection_not_confirmed:' + code)
            proof['purchase'] = current_receipt(selected.id, expected[1], expected[2])
        line = Line(selected.id, Decimal(item['quantity']), item['price_kopeks'],
                    Decimal(item['discount_percent']))
        total += line_total(line); chosen.append((line, proof))
    if os.getenv('CARTRIDGE_API_KEY'):
        raise RuntimeError('bundle_catalogue_present_but_not_connected')
    stamp = datetime.fromisoformat(source['created_at'].replace('Z', '+00:00'))
    payload = build_payload(platform=platform, account=account, external_id=number,
        lines=[x[0] for x in chosen], expected_kopeks=total, available=False,
        now=datetime.now(timezone.utc), market_created_at=stamp)
    return {'source': source, 'selected': [x[1] for x in chosen], 'total_kopeks': total,
            'manual_reason': 'bundle_composition_unconfirmed', 'payload': payload}


def create_one(platform: str, account: str, number: str) -> dict:
    folder = OUT / (platform + '_' + account + '_' + number)
    if (folder / 'intent.json').exists():
        raise RuntimeError('prior_intent_requires_reconciliation')
    first = inspect(platform, account, number)
    save(folder / 'preflight.json', first)
    second = inspect(platform, account, number)
    if first['source'] != second['source'] or first['selected'] != second['selected'] or first['total_kopeks'] != second['total_kopeks']:
        raise RuntimeError('source_or_goods_changed_before_post')
    # The prior two checks must finish before this durable intent and one POST.
    save(folder / 'intent.json', {'at': datetime.now(timezone.utc).isoformat(),
                                 'key': [platform, account, number], 'payload': second['payload']}, exclusive=True)
    try:
        response = requests.post(ms_api.BASE + '/entity/customerorder',
            headers={'Authorization': 'Bearer ' + os.environ['MOYSKLAD_TOKEN'],
                     'Content-Type': 'application/json'}, json=second['payload'], timeout=(5, 45))
        save(folder / 'response.json', {'status': response.status_code,
                                       'body': response.json() if response.ok else response.text[:1000]})
    except (requests.Timeout, requests.ConnectionError) as exc:
        found = find_customerorder(number)
        save(folder / 'uncertain.json', {'error': type(exc).__name__,
                                        'found_ms_id': found['id'] if found else None})
        raise RuntimeError('post_uncertain_no_retry') from None
    if response.status_code not in (200, 201): raise RuntimeError('ms_post_rejected')
    created = response.json(); order = ms_api.get('/entity/customerorder/' + created['id'],
        {'expand': 'state,store,organization,agent,positions.assortment'})
    save(folder / 'readback.json', order)
    if (order.get('name') != number or order.get('sum') != second['total_kopeks']
            or order.get('state', {}).get('name') != 'Ожидает подтверждения'
            or ms_api.meta_id(order, 'store') != STORE[account]
            or len(order.get('positions', {}).get('rows', [])) != len(second['selected'])
            or order.get('demands')):
        raise RuntimeError('ms_readback_mismatch_stop_batch')
    return {'order': number, 'platform': platform, 'account': account,
            'ms_id': created['id'], 'sum_kopeks': order['sum'],
            'status': order['state']['name'], 'url': order['meta'].get('uuidHref')}


def run(apply: bool) -> dict:
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    results = []
    for platform, account, number in SCOPE:
        if find_customerorder(number):
            result = {'order': number, 'result': 'already_in_ms'}
        else:
            try:
                if apply:
                    result = {'result': 'created', **create_one(platform, account, number)}
                else:
                    check = inspect(platform, account, number)
                    result = {'order': number, 'result': 'ready', 'sum_kopeks': check['total_kopeks'],
                              'products': [p['card_code'] for p in check['selected']]}
            except RuntimeError as exc:
                reason = str(exc)
                if reason.startswith(('product_selection:', 'exact_base_product_card_missing:',
                                      'wb_full_price_unavailable', 'already_in_ms:')):
                    result = {'order': number, 'result': 'excluded', 'reason': reason}
                else:
                    result = {'order': number, 'result': 'stopped', 'reason': reason}
                    results.append(result); save(OUT / 'run.json', {'results': results, 'stopped': True})
                    raise
        results.append(result)
        save(OUT / 'run.json', {'results': results, 'stopped': False})
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return {'scope': len(SCOPE), 'created': sum(x['result'] == 'created' for x in results),
            'excluded': sum(x['result'] == 'excluded' for x in results),
            'already_in_ms': sum(x['result'] == 'already_in_ms' for x in results)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.apply), ensure_ascii=False))

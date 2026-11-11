# поток: inv
"""One WB order with explicit user-approved Orders API price fallback."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import requests

from core import ms_api_control
from core import ms_api
from ops.fbs_ms_import_batch10 import OUT as BATCH_OUT, START, base_code, stock_for
from ops.fbs_ms_import_pilot import save
from ops.fbs_ms_import_read import find_customerorder, product_cards
from ops.fbs_ms_order_payload import Line, STORE, build_payload

NUMBER = '6176952570'
MARKET_CODE = '02741'
CARD_CODE = '0274t2'
EXPECTED_BUYER_KOPEKS = 189800


def wb_order() -> dict:
    h = {'Authorization': os.environ['WB_TOKEN_ACC1']}; cursor = 0; found = None
    for _ in range(50):
        r = requests.get('https://marketplace-api.wildberries.ru/api/v3/orders',
            headers=h, params={'limit': 20, 'next': cursor, 'dateFrom': int(START.timestamp()),
                                'dateTo': int(datetime.now(timezone.utc).timestamp())}, timeout=(5, 25))
        r.raise_for_status(); data = r.json(); rows = data.get('orders')
        if not isinstance(rows, list): raise RuntimeError('wb_order_schema')
        for row in rows:
            if row.get('id') == int(NUMBER):
                if found is not None and found != row: raise RuntimeError('wb_order_conflict')
                found = row
        if len(rows) < 20: break
        nxt = data.get('next')
        if not isinstance(nxt, int) or nxt <= cursor: raise RuntimeError('wb_cursor_stalled')
        cursor = nxt
    else: raise RuntimeError('wb_page_budget')
    if not found or found.get('deliveryType') != 'fbs' or found.get('article') != MARKET_CODE:
        raise RuntimeError('wb_identity_or_scope_changed')
    r = requests.post('https://marketplace-api.wildberries.ru/api/v3/orders/status',
        headers=h, json={'orders': [int(NUMBER)]}, timeout=(5, 25))
    r.raise_for_status(); statuses = r.json().get('orders') or []
    if (len(statuses) != 1 or statuses[0].get('id') != int(NUMBER)
            or (statuses[0].get('supplierStatus'), statuses[0].get('wbStatus')) != ('new', 'waiting')):
        raise RuntimeError('wb_status_changed')
    rid = found.get('rid')
    if not rid: raise RuntimeError('wb_rid_missing')
    r = requests.get('https://statistics-api.wildberries.ru/api/v1/supplier/orders',
        headers=h, params={'dateFrom': '2026-11-02T00:00:00', 'flag': 1}, timeout=(5, 40))
    r.raise_for_status(); matches = [x for x in r.json() if x.get('srid') == rid]
    if matches: raise RuntimeError('wb_full_price_now_available_reassess')
    if str(found.get('currencyCode')) != '643' or found.get('price') != EXPECTED_BUYER_KOPEKS:
        raise RuntimeError('wb_buyer_price_changed_or_not_rub')
    return {'number': NUMBER, 'market_code': MARKET_CODE, 'rid': rid,
            'created_at': found['createdAt'], 'status': ('new', 'waiting'),
            'price_kopeks': EXPECTED_BUYER_KOPEKS,
            'price_source': 'WB Orders API price; full priceWithDisc absent by rid/srid'}


def inspect() -> dict:
    market = wb_order()
    existing = find_customerorder(NUMBER)
    if existing: raise RuntimeError('already_in_ms:' + existing['id'])
    base = base_code(market['market_code'])
    cards = product_cards(base)
    matches = [c for c in cards if c.get('code') == CARD_CODE]
    if len(matches) != 1: raise RuntimeError('approved_card_missing_or_ambiguous')
    card = matches[0]
    stock = stock_for([card], STORE['acc1'])[card['id']]
    available = Decimal(str(stock['stock'])) - Decimal(str(stock['reserve']))
    if available < 1: raise RuntimeError('approved_card_stock_unavailable')
    payload = build_payload(platform='wb', account='acc1', external_id=NUMBER,
        lines=[Line(card['id'], Decimal(1), market['price_kopeks'])],
        expected_kopeks=market['price_kopeks'], available=False,
        now=datetime.now(timezone.utc),
        market_created_at=datetime.fromisoformat(market['created_at'].replace('Z', '+00:00')))
    return {'market': market, 'selected_card_id': card['id'],
            'selected_card_code': card['code'], 'stock': stock, 'payload': payload}


def run(apply: bool):
    folder = BATCH_OUT / ('wb_acc1_' + NUMBER + '_buyer_price_fallback')
    if (folder / 'intent.json').exists():
        raise RuntimeError('prior_intent_requires_reconciliation')
    first = inspect(); save(folder / 'preflight.json', first)
    if not apply:
        return {'order': NUMBER, 'mode': 'check', 'price_kopeks': EXPECTED_BUYER_KOPEKS,
                'card': first['selected_card_code'], 'free':
                str(Decimal(str(first['stock']['stock'])) - Decimal(str(first['stock']['reserve'])))}
    second = inspect()
    if (first['market'] != second['market'] or first['selected_card_id'] != second['selected_card_id']
            or first['stock'] != second['stock']):
        raise RuntimeError('source_card_or_stock_changed_before_post')
    save(folder / 'intent.json', {'at': datetime.now(timezone.utc).isoformat(),
                                 'key': ['wb', 'wb_acc1', NUMBER], 'payload': second['payload'],
                                 'price_source': second['market']['price_source']}, exclusive=True)
    try:
        r = ms_api_control.request("POST", ms_api.BASE + '/entity/customerorder', sender=requests.post, client="operations-direct", kind="operational",
            headers={'Authorization': 'Bearer ' + os.environ['MOYSKLAD_TOKEN'],
                     'Content-Type': 'application/json'}, json=second['payload'], timeout=(5, 45))
        save(folder / 'response.json', {'status': r.status_code,
                                       'body': r.json() if r.ok else r.text[:1000]})
    except (requests.Timeout, requests.ConnectionError) as exc:
        existing = find_customerorder(NUMBER)
        save(folder / 'uncertain.json', {'error': type(exc).__name__,
                                        'found_ms_id': existing['id'] if existing else None})
        raise RuntimeError('post_uncertain_no_retry') from None
    if r.status_code not in (200, 201): raise RuntimeError('ms_post_rejected')
    created = r.json()
    o = ms_api.get('/entity/customerorder/' + created['id'],
                   {'expand': 'state,store,organization,agent,positions.assortment'})
    save(folder / 'readback.json', o)
    positions = o.get('positions', {}).get('rows', [])
    if (o.get('name') != NUMBER or o.get('sum') != EXPECTED_BUYER_KOPEKS
            or o.get('state', {}).get('name') != 'Ожидает подтверждения'
            or ms_api.meta_id(o, 'store') != STORE['acc1'] or len(positions) != 1
            or positions[0]['assortment'].get('code') != CARD_CODE
            or positions[0].get('vat') != 5 or o.get('demands') or o.get('deliveryPlannedMoment')):
        raise RuntimeError('ms_readback_mismatch_stop')
    return {'order': NUMBER, 'mode': 'apply', 'ms_id': created['id'],
            'sum_kopeks': o['sum'], 'status': o['state']['name'],
            'card': CARD_CODE, 'url': o['meta'].get('uuidHref')}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.apply), ensure_ascii=False))

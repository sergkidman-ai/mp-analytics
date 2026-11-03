# поток: inv
"""One WB FBS pilot import; explicit --apply, one-shot MS POST, no WB writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import requests

from core import ms_api
from ops.fbs_ms_import_pilot import ARTIFACT, save
from ops.fbs_ms_import_read import find_customerorder, product_cards, warehouse_stock
from ops.fbs_ms_import_policy import Card, choose_card
from ops.fbs_ms_order_payload import Line, STORE, build_payload
from prices.profiles import STORE_REMOTE

ORDER_ID = '6176116763'
SOURCE_START = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
DOC_RE = re.compile(r'^(?P<key>.+)_(?P<day>\d{4}-\d{2}-\d{2})(?:_p\d+)?$')


def wb_order(number: str) -> dict:
    if number != ORDER_ID:
        raise ValueError('pilot_order_id_is_fixed')
    token = os.environ['WB_TOKEN_ACC1']
    headers = {'Authorization': token}
    cursor = 0
    order = None
    for _ in range(50):
        r = requests.get('https://marketplace-api.wildberries.ru/api/v3/orders',
                         headers=headers, params={'limit': 20, 'next': cursor,
                         'dateFrom': int(SOURCE_START.timestamp()),
                         'dateTo': int(datetime.now(timezone.utc).timestamp())}, timeout=(5, 25))
        r.raise_for_status()
        data = r.json()
        rows = data.get('orders')
        if not isinstance(rows, list):
            raise RuntimeError('wb_orders_schema')
        for row in rows:
            if row.get('id') == int(number):
                if order is not None and order != row:
                    raise RuntimeError('wb_order_conflict')
                order = row
        if len(rows) < 20:
            break
        next_cursor = data.get('next')
        if not isinstance(next_cursor, int) or next_cursor <= cursor:
            raise RuntimeError('wb_cursor_stalled')
        cursor = next_cursor
    else:
        raise RuntimeError('wb_page_budget')
    if not order or order.get('deliveryType') != 'fbs':
        raise RuntimeError('wb_order_missing_or_not_fbs')
    status = requests.post('https://marketplace-api.wildberries.ru/api/v3/orders/status',
                           headers=headers, json={'orders': [int(number)]}, timeout=(5, 25))
    status.raise_for_status()
    states = status.json().get('orders') or []
    if len(states) != 1 or states[0].get('id') != int(number):
        raise RuntimeError('wb_status_identity')
    if (states[0].get('supplierStatus'), states[0].get('wbStatus')) != ('new', 'waiting'):
        raise RuntimeError('wb_order_not_new_waiting')
    if not order.get('article') or not order.get('rid') or not order.get('createdAt'):
        raise RuntimeError('wb_order_identity_incomplete')
    stats = requests.get('https://statistics-api.wildberries.ru/api/v1/supplier/orders',
                         headers=headers, params={'dateFrom': '2026-11-02T00:00:00', 'flag': 1},
                         timeout=(5, 40))
    stats.raise_for_status()
    hits = [x for x in stats.json() if x.get('srid') == order['rid']]
    if len(hits) != 1 or hits[0].get('isCancel') is not False:
        raise RuntimeError('wb_full_price_not_unique_or_cancelled')
    price = Decimal(str(hits[0].get('priceWithDisc')))
    kopeks = price * 100
    if kopeks <= 0 or kopeks != kopeks.to_integral_value():
        raise RuntimeError('wb_full_price_invalid')
    return {'number': number, 'article': order['article'], 'rid': order['rid'],
            'order_uid': order.get('orderUid'), 'created_at': order['createdAt'],
            'supplier_status': 'new', 'wb_status': 'waiting',
            'buyer_price_kopeks': order.get('price'), 'price_kopeks': int(kopeks)}


def current_purchase_documents() -> list[tuple[str, str, dict]]:
    """Latest active Remote receipts for every supplier, without a date cutoff.

    Supplier eligibility is checked on the product card by the selection policy.
    Do not hide Sakura, Bulat or other suppliers behind a receipt-name allowlist.
    """
    store = ms_api.ref('store', STORE_REMOTE)['meta']['href']
    docs = []
    offset = 0
    while True:
        response = ms_api.get('/entity/enter', {'limit': 1000, 'offset': offset,
                                              'filter': f'store={store}'})
        page = response.get('rows', [])
        size = response.get('meta', {}).get('size')
        if (not isinstance(size, int) or offset + len(page) > size
                or (offset < size and not page)):
            raise RuntimeError('enter_list_incomplete')
        docs.extend(page)
        offset += len(page)
        if offset == size:
            break
        if offset >= 10000:
            raise RuntimeError('enter_page_budget')
    if len({d['id'] for d in docs}) != len(docs):
        raise RuntimeError('enter_list_duplicate')
    groups = {}
    for doc in docs:
        match = DOC_RE.match(doc.get('name', ''))
        if (not match or '_old' in doc.get('name', '')
                or doc.get('applicable') is False):
            continue
        doc_store = ms_api.meta_id(doc, 'store')
        if doc_store and doc_store != STORE_REMOTE:
            raise RuntimeError('enter_store_mismatch')
        key, day = match['key'], match['day']
        if key not in groups or day > groups[key][0]:
            groups[key] = (day, [doc])
        elif day == groups[key][0]:
            groups[key][1].append(doc)
    if not groups:
        raise RuntimeError('supplier_enter_group_missing')
    return [(key, day, doc) for key, (day, latest) in sorted(groups.items())
            for doc in sorted(latest, key=lambda d: d['id'])]


def purchase_documents_digest(documents: list[tuple[str, str, dict]]) -> str:
    # Invalidate the old four-supplier cache even when those receipts are unchanged.
    provenance = [(key, day, doc['id'], doc['updated']) for key, day, doc in documents]
    return hashlib.sha256(json.dumps(['all-supplier-prices-v2', sorted(provenance)]).encode()).hexdigest()


def current_purchase_prices(card_ids: set[str] | None) -> tuple[dict[str, int], str]:
    documents = current_purchase_documents()
    prices = {}
    ambiguous = set()
    for _, _, doc in documents:
        page = ms_api.get('/entity/enter/' + doc['id'] + '/positions', {'limit': 1000})
        if page['meta']['size'] != len(page['rows']):
            raise RuntimeError('enter_positions_incomplete')
        for pos in page['rows']:
            pid = pos.get('assortment', {}).get('meta', {}).get('href', '').rsplit('/', 1)[-1]
            if card_ids is not None and pid not in card_ids:
                continue
            try:
                amount = Decimal(str(pos.get('price')))
                valid = amount.is_finite() and amount > 0 and amount == amount.to_integral_value()
            except (InvalidOperation, ValueError):
                valid = False
            if not valid:
                if card_ids is not None:
                    raise RuntimeError('enter_price_invalid')
                ambiguous.add(pid)
                prices.pop(pid, None)
                continue
            if pid in prices and prices[pid] != int(amount):
                if card_ids is not None:
                    raise RuntimeError('enter_price_conflict')
                ambiguous.add(pid)
                prices.pop(pid, None)
            elif pid not in ambiguous:
                prices[pid] = int(amount)
    digest = purchase_documents_digest(documents)
    return prices, digest


def inspect(number: str) -> dict:
    market = wb_order(number)
    existing = find_customerorder(number)
    if existing:
        raise RuntimeError('already_in_ms:' + existing['id'])
    code = market['article']
    cards = product_cards(code)
    stock = warehouse_stock(STORE['acc1'], cards)
    prices, docs_digest = current_purchase_prices({c['id'] for c in cards})
    domain = [Card(c['id'], c['externalCode'], ms_api.meta_id(c, 'supplier') or '',
                   (c.get('code') or '').removeprefix(code).casefold(), prices.get(c['id']),
                   Decimal(str(stock[c['id']]['stock'])),
                   Decimal(str(stock[c['id']]['reserve'])), c.get('archived', False))
              for c in cards]
    selected, available = choose_card(domain, code, Decimal(1))
    if available:
        raise RuntimeError('pilot_stock_changed_reassess_policy')
    if os.getenv('CARTRIDGE_API_KEY'):
        raise RuntimeError('current_bundle_lookup_not_connected')
    payload = build_payload(platform='wb', account='acc1', external_id=number,
                            lines=[Line(selected.id, Decimal(1), market['price_kopeks'])],
                            expected_kopeks=market['price_kopeks'], available=False,
                            now=datetime.now(timezone.utc),
                            market_created_at=datetime.fromisoformat(market['created_at'].replace('Z', '+00:00')))
    return {'market': market, 'selected_product_id': selected.id,
            'selected_code': next(c['code'] for c in cards if c['id'] == selected.id),
            'purchase_kopeks': selected.purchase_kopeks, 'docs_digest': docs_digest,
            'stock': stock[selected.id], 'manual_reason': 'no_stock_and_bundle_composition_unconfirmed',
            'payload': payload}


def run(number: str, apply: bool):
    folder = ARTIFACT / ('wb_acc1_' + number)
    if (folder / 'intent.json').exists():
        raise RuntimeError('prior_write_intent_requires_manual_reconciliation')
    first = inspect(number)
    save(folder / 'preflight.json', first)
    if not apply:
        return {'order': number, 'mode': 'check', 'ms_status': 'Ожидает подтверждения',
                'sku': first['market']['article'], 'sum_kopeks': first['market']['price_kopeks'],
                'product_code': first['selected_code'], 'purchase_kopeks': first['purchase_kopeks']}
    second = inspect(number)
    for key in ('market', 'selected_product_id', 'purchase_kopeks', 'docs_digest'):
        if first[key] != second[key]:
            raise RuntimeError('source_or_product_changed_before_post')
    save(folder / 'intent.json', {'at': datetime.now(timezone.utc).isoformat(),
                                 'key': ['wb', 'wb_acc1', number], 'payload': second['payload']},
         exclusive=True)
    try:
        response = requests.post(ms_api.BASE + '/entity/customerorder',
                                 headers={'Authorization': 'Bearer ' + os.environ['MOYSKLAD_TOKEN'],
                                          'Content-Type': 'application/json'},
                                 json=second['payload'], timeout=(5, 45))
        save(folder / 'response.json', {'status': response.status_code,
                                        'body': response.json() if response.ok else response.text[:1000]})
    except (requests.Timeout, requests.ConnectionError) as exc:
        found = find_customerorder(number)
        save(folder / 'uncertain.json', {'error': type(exc).__name__,
                                        'found_ms_id': found['id'] if found else None})
        raise RuntimeError('post_uncertain_no_retry') from None
    if response.status_code not in (200, 201):
        raise RuntimeError('ms_post_rejected_or_uncertain')
    created = response.json()
    readback = ms_api.get('/entity/customerorder/' + created['id'],
                          {'expand': 'state,store,organization,agent,positions.assortment'})
    save(folder / 'readback.json', readback)
    if (readback['name'] != number or readback['sum'] != second['market']['price_kopeks']
            or readback['state']['name'] != 'Ожидает подтверждения'):
        raise RuntimeError('ms_readback_mismatch')
    return {'order': number, 'mode': 'apply', 'ms_id': created['id'],
            'ms_status': readback['state']['name'], 'sum_kopeks': readback['sum'],
            'ms_url': readback['meta'].get('uuidHref')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('number')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.number, args.apply), ensure_ascii=False))

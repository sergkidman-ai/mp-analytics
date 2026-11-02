# поток: inv
"""One Yandex FBS pilot import; read-only default, explicit --apply."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import requests

from core import ms_api
from ops.fbs_ms_import_pilot import ARTIFACT, save
from ops.fbs_ms_import_read import find_customerorder, product_cards, warehouse_stock
from ops.fbs_ms_import_policy import Card, choose_card
from ops.fbs_ms_order_payload import Line, STORE, build_payload


def yandex_order(number: str) -> dict:
    business = os.environ['YANDEX_BUSINESS_ID_ACC1']
    if not business.isdigit() or not number.isdigit():
        raise ValueError('invalid_yandex_identity')
    r = requests.post(f'https://api.partner.market.yandex.ru/v1/businesses/{business}/orders',
                      headers={'Api-Key': os.environ['YANDEX_API_KEY_ACC1']},
                      json={'orderIds': [int(number)], 'programTypes': ['FBS'], 'fake': False},
                      params={'limit': 20}, timeout=(5, 25))
    r.raise_for_status()
    rows = r.json().get('orders', [])
    if len(rows) != 1 or str(rows[0].get('orderId')) != number:
        raise RuntimeError('yandex_order_identity_or_count')
    o = rows[0]
    if o.get('programType') != 'FBS' or o.get('fake') is not False:
        raise RuntimeError('yandex_not_real_fbs')
    if (o.get('status'), o.get('substatus')) != ('PROCESSING', 'STARTED'):
        raise RuntimeError('yandex_not_started_processing')
    items = o.get('items') or []
    if len(items) != 1 or items[0].get('count') != 1:
        raise RuntimeError('pilot_requires_one_item')
    item = items[0]
    prices = item.get('prices') or {}
    if not item.get('offerId') or 'payment' not in prices:
        raise RuntimeError('yandex_item_price_missing')
    total = Decimal(0)
    for key in ('payment', 'subsidy', 'cashback'):
        value = prices.get(key)
        if value is None:
            continue
        if value.get('currencyId') not in ('RUR', 'RUB'):
            raise RuntimeError('yandex_non_rub')
        amount = Decimal(str(value['value']))
        if not amount.is_finite() or amount < 0:
            raise RuntimeError('yandex_invalid_amount')
        total += amount
    kopeks = total * 100
    if kopeks <= 0 or kopeks != kopeks.to_integral_value():
        raise RuntimeError('yandex_amount_not_kopeks')
    return {'number': number, 'business_id': business, 'campaign_id': o['campaignId'],
            'status': o['status'], 'substatus': o['substatus'], 'offer_id': item['offerId'],
            'quantity': 1, 'price_kopeks': int(kopeks), 'created_at': o['creationDate']}


def inspect(number: str) -> dict:
    market = yandex_order(number)
    existing = find_customerorder(number)
    if existing:
        raise RuntimeError('already_in_ms:' + existing['id'])
    code = market['offer_id']
    cards = product_cards(code)
    stock = warehouse_stock(STORE['acc1'], cards)
    domain = [Card(c['id'], c['externalCode'], ms_api.meta_id(c, 'supplier') or '',
                   (c.get('code') or '').removeprefix(code).casefold(), None,
                   Decimal(str(stock[c['id']]['stock'])),
                   Decimal(str(stock[c['id']]['reserve'])), c.get('archived', False))
              for c in cards]
    selected, _ = choose_card(domain, code, Decimal(1))
    if os.getenv('CARTRIDGE_API_KEY'):
        raise RuntimeError('current_bundle_lookup_not_connected')
    payload = build_payload(platform='yandex', account='acc1', external_id=number,
                            lines=[Line(selected.id, Decimal(1), market['price_kopeks'])],
                            expected_kopeks=market['price_kopeks'], available=False,
                            now=datetime.now(timezone.utc),
                            market_created_at=datetime.fromisoformat(market['created_at']))
    return {'market': market, 'selected_product_id': selected.id,
            'selected_code': next(c['code'] for c in cards if c['id'] == selected.id),
            'stock': stock[selected.id], 'manual_reason': 'bundle_composition_unconfirmed',
            'payload': payload}


def run(number: str, apply: bool):
    folder = ARTIFACT / ('ya_acc1_' + number)
    if (folder / 'intent.json').exists():
        raise RuntimeError('prior_write_intent_requires_manual_reconciliation')
    first = inspect(number)
    save(folder / 'preflight.json', first)
    if not apply:
        return {'order': number, 'mode': 'check', 'ms_status': 'Ожидает подтверждения',
                'sku': first['market']['offer_id'], 'sum_kopeks': first['market']['price_kopeks'],
                'product_code': first['selected_code'], 'reason': first['manual_reason']}
    second = inspect(number)
    if first['market'] != second['market'] or first['selected_product_id'] != second['selected_product_id']:
        raise RuntimeError('market_or_product_changed_before_post')
    save(folder / 'intent.json', {'at': datetime.now(timezone.utc).isoformat(),
                                 'key': ['yandex', 'ya_acc1', number], 'payload': second['payload']},
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

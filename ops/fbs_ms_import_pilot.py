# поток: inv
"""One Ozon FBS pilot import; explicit --apply, one-shot MS POST, no MP writes.

The order stays waiting when a current bundle catalogue cannot prove its
composition. This is intentionally narrower than a general importer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import requests
from dotenv import load_dotenv

from core import ms_api_control
from core import ms_api
from ops.fbs_ms_import_read import find_customerorder, product_cards, warehouse_stock
from ops.fbs_ms_import_policy import Card, choose_card
from ops.fbs_ms_order_payload import Line, STORE, build_payload

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
ARTIFACT = ROOT / 'docs/reports/fbs_ms_recovery_20261102/pilots'


def save(path: Path, data: dict, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, ensure_ascii=False, indent=2, default=str)


def ozon_post(number: str) -> dict:
    h = {'Client-Id': os.environ['OZON_CLIENT_ID_ACC1'],
         'Api-Key': os.environ['OZON_API_KEY_ACC1']}
    r = requests.post('https://api-seller.ozon.ru/v3/posting/fbs/get',
                      headers=h, json={'posting_number': number, 'with': {'financial_data': True}},
                      timeout=(5, 25))
    r.raise_for_status()
    p = r.json().get('result')
    if not isinstance(p, dict) or p.get('posting_number') != number:
        raise RuntimeError('ozon_identity_mismatch')
    if p.get('status') in ('cancelled', 'delivered', 'delivering'):
        raise RuntimeError('ozon_order_not_importable')
    products = p.get('products') or []
    financial = (p.get('financial_data') or {}).get('products') or []
    if len(products) != 1 or len(financial) != 1 or products[0].get('quantity') != 1:
        raise RuntimeError('pilot_requires_one_item')
    product, money = products[0], financial[0]
    if str(product.get('sku')) != str(money.get('product_id')):
        raise RuntimeError('ozon_product_finance_mismatch')
    old, final = Decimal(str(money.get('old_price'))), Decimal(str(money.get('price')))
    if old <= 0 or old != final or money.get('total_discount_value') not in (0, 0.0, '0', None):
        raise RuntimeError('pilot_discount_requires_separate_validation')
    created = p.get('in_process_at') or p.get('created_at')
    if not created:
        raise RuntimeError('ozon_creation_time_missing')
    return {'number': number, 'status': p['status'], 'offer_id': product['offer_id'],
            'sku': str(product['sku']), 'quantity': 1,
            'price_kopeks': int(old * 100), 'created_at': created}


def inspect(number: str) -> dict:
    market = ozon_post(number)
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
    selected, available = choose_card(domain, code, Decimal(1))
    # A current TC composition is unavailable on this VPS. Preserve the exact MP
    # code as one existing MS card and leave manual handling to a person.
    if not os.getenv('CARTRIDGE_API_KEY'):
        available = False
        reason = 'bundle_composition_unconfirmed'
    else:
        raise RuntimeError('current_bundle_lookup_not_connected')
    payload = build_payload(platform='ozon', account='acc1', external_id=number,
                            lines=[Line(selected.id, Decimal(1), market['price_kopeks'])],
                            expected_kopeks=market['price_kopeks'], available=available,
                            now=datetime.now(timezone.utc),
                            market_created_at=datetime.fromisoformat(market['created_at'].replace('Z', '+00:00')))
    return {'market': market, 'selected_product_id': selected.id,
            'selected_code': next(c['code'] for c in cards if c['id'] == selected.id),
            'stock': stock[selected.id], 'manual_reason': reason, 'payload': payload}


def run(number: str, apply: bool):
    folder = ARTIFACT / ('oz_acc1_' + number)
    if (folder / 'intent.json').exists():
        raise RuntimeError('prior_write_intent_requires_manual_reconciliation')
    first = inspect(number)
    save(folder / 'preflight.json', first)
    if not apply:
        return {'order': number, 'mode': 'check', 'ms_status': 'Ожидает подтверждения',
                'sku': first['market']['offer_id'], 'sum_kopeks': first['market']['price_kopeks'],
                'product_code': first['selected_code'], 'reason': first['manual_reason']}
    second = inspect(number)
    if {k: first['market'][k] for k in first['market']} != second['market'] or first['selected_product_id'] != second['selected_product_id']:
        raise RuntimeError('market_or_product_changed_before_post')
    save(folder / 'intent.json', {'at': datetime.now(timezone.utc).isoformat(),
                                 'key': ['ozon', 'oz_acc1', number], 'payload': second['payload']},
         exclusive=True)
    try:
        response = ms_api_control.request("POST", ms_api.BASE + '/entity/customerorder', sender=requests.post, client="operations-direct", kind="operational",
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
    if readback['name'] != number or readback['sum'] != second['market']['price_kopeks'] or readback['state']['name'] != 'Ожидает подтверждения':
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

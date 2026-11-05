# поток: inv
"""Chronological five-order FBS recovery batch. Only MS customerorder POSTs."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from core import ms_api
from ops.fbs_ms_import_batch10 import base_code
from ops.fbs_ms_import_pilot import save
from ops.fbs_ms_import_policy import Card, SelectionError, choose_bundle_plan, choose_card, choose_ready_set, tier, priority
from ops.ms_supplier_priorities import POLICY_VERSION
from ops.fbs_ms_import_read import find_customerorder, product_cards, warehouse_stock
from ops.fbs_ms_set_catalog import components as set_components
from ops.fbs_ms_set_prices import split_kit_price
from ops.fbs_ms_order_payload import Line, STORE, build_payload, line_total
from prices.profiles import STORE_REMOTE

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
OUT = ROOT / 'docs/reports/fbs_ms_recovery_20261102/all_import'
USER_PRICE_ORDERS = {'6178120612', '6179632720', '6179632722'}
SCOPE = tuple(n for n in os.environ.get('FBS_IMPORT_SCOPE', '').split(',') if n)


def _stock(cards: list[dict], store_id: str) -> dict[str, dict]:
    result = {}
    for start in range(0, len(cards), 20):
        result.update(warehouse_stock(store_id, cards[start:start + 20]))
    return result


def _quantities(rows: list[dict]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        uid = row['assortment'].get('id') or row['assortment']['meta']['href'].rsplit('/', 1)[-1]
        result[uid] += Decimal(str(row['quantity']))
    return dict(result)


def _unreserved_promises(selected: list[dict], positions: list[dict],
                         own_store: str) -> dict[tuple[str, str], Decimal]:
    result: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for item in selected:
        store_id = item.get('stock_store_id')
        if store_id:
            result[(store_id, item['card_id'])] += Decimal(item['quantity'])
    for position in positions:
        cid = position['assortment'].get('id') or position['assortment']['meta']['href'].rsplit('/', 1)[-1]
        key = (own_store, cid)
        if key in result:
            result[key] -= Decimal(str(position.get('reserve') or 0))
    return {key: max(Decimal(0), qty) for key, qty in result.items()}


def _confirmed_set_offer(market_code: str, base: str, cards: list[dict],
                         account: str | None = None) -> bool:
    """A four-digit set family may also contain unrelated supplier variants."""
    if market_code == base or market_code.lower() in (base + 'msk', base + 'dsk'):
        return True
    exact = [c for c in cards if (c.get('code') or '').lower() == market_code.lower()]
    if exact:
        return (len(exact) == 1 and
                bool(re.search(r'\b(?:набор\w*|комплект\w*)\b', exact[0].get('name') or '', re.I)))
    # Dsquare publishes TC set copies as <four-digit externalCode><8-char ID>.
    # The verified TC composition is checked by the caller. Require the exact
    # existing dsk parent too; arbitrary supplier/numeric variants stay manual.
    parents = [c for c in cards if c.get('externalCode') == base
               and (c.get('code') or '').lower() == base + 'dsk'
               and not c.get('archived')]
    return (account == 'acc2' and len(parents) == 1
            and bool(re.fullmatch(re.escape(base) + r'[A-Z0-9]{8}', market_code))
            and bool(re.search(r'[A-Z]', market_code[4:])))


def _kit_lines(item: dict, code: str, account: str, prices: dict[str, int],
               composition: list[tuple[str, int]],
               promised: dict[tuple[str, str], Decimal] | None = None) -> tuple[list[Line], list[dict]]:
    quantity = Decimal(item['quantity'])
    if quantity != quantity.to_integral_value():
        raise SelectionError('set_quantity_not_integer')
    all_cards = []
    for component, _ in composition:
        cards = product_cards(component)
        if not cards:
            raise SelectionError('set_component_card_missing:' + component)
        all_cards.extend(cards)
    if len({c['id'] for c in all_cards}) != len(all_cards):
        raise SelectionError('set_component_cards_duplicate')
    own_stock = _stock(all_cards, STORE[account])
    remote_stock = _stock(all_cards, STORE_REMOTE)
    promised = promised or {}
    own, remote = [], []
    by_id = {c['id']: c for c in all_cards}
    for c in all_cards:
        component = c['externalCode']
        common = (c['id'], component, ms_api.meta_id(c, 'supplier') or '',
                  (c.get('code') or '').removeprefix(component).lower(), prices.get(c['id']))
        own.append(Card(*common, Decimal(str(own_stock[c['id']]['stock'])),
                        Decimal(str(own_stock[c['id']]['reserve'])) + promised.get((STORE[account], c['id']), 0),
                        c.get('archived', False)))
        remote.append(Card(*common, Decimal(str(remote_stock[c['id']]['stock'])),
                           Decimal(str(remote_stock[c['id']]['reserve'])) + promised.get((STORE_REMOTE, c['id']), 0),
                           c.get('archived', False)))
    needs = [(component, Decimal(count) * quantity) for component, count in composition]
    plan = choose_bundle_plan(needs, own, remote)
    ids = [card.id for card, count, _ in plan for _ in range(int(count))]
    lines = split_kit_price(ids, int(quantity), item['price_kopeks'],
                            Decimal(item['discount_percent']))
    proof = []
    for card, count, available in plan:
        source_stock = own_stock if available else remote_stock
        for _ in range(int(count)):
            proof.append({'market_code': item['market_code'], 'base_code': card.external_code,
                          'component_of': code, 'card_id': card.id,
                          'card_code': by_id[card.id].get('code'),
                          'supplier_id': card.supplier_id, 'brand': card.brand,
                          'stock': source_stock[card.id], 'available': available,
                          'quantity': '1',
                          'tier': tier(card), 'priority': priority(card),
                          'selection_policy': POLICY_VERSION,
                          'supplier_available': str(card.available),
                          'purchase_kopeks': card.purchase_kopeks,
                          'manual_fallback': False,
                          'stock_store_id': STORE[account] if available else STORE_REMOTE})
    return lines, proof


def _ready_set_lines(item: dict, code: str, account: str, cards: list[dict],
                     prices: dict[str, int], promised: dict) -> tuple[list[Line], list[dict]] | None:
    if not cards:
        return None
    store = STORE[account]
    stock = _stock(cards, store)
    domain = [Card(c['id'], c['externalCode'], ms_api.meta_id(c, 'supplier') or '',
                   (c.get('code') or '').removeprefix(code).lower(), prices.get(c['id']),
                   Decimal(str(stock[c['id']]['stock'])),
                   Decimal(str(stock[c['id']]['reserve'])) + promised.get((store, c['id']), 0),
                   c.get('archived', False)) for c in cards]
    chosen = choose_ready_set(domain, code, Decimal(item['quantity']))
    if chosen is None:
        return None
    proof = {'market_code': item['market_code'], 'base_code': code, 'card_id': chosen.id,
             'card_code': next(c.get('code') for c in cards if c['id'] == chosen.id),
             'quantity': str(item['quantity']), 'stock': stock[chosen.id], 'available': True,
             'stock_store_id': store, 'supplier_id': chosen.supplier_id,
             'tier': tier(chosen), 'priority': priority(chosen), 'purchase_kopeks': chosen.purchase_kopeks,
             'selection_policy': POLICY_VERSION, 'manual_fallback': False, 'manual_parent': False,
             'whole_set': True}
    line = Line(chosen.id, Decimal(item['quantity']), item['price_kopeks'],
                Decimal(item['discount_percent']))
    return [line], [proof]


def source_index():
    scan = json.loads((OUT / 'source_scan.json').read_text())
    rows = [r for batch in scan['accounts'].values() for r in batch]
    index = {r['number']: r for r in rows}
    if len(index) != len(rows) or any(n not in index for n in SCOPE):
        raise RuntimeError('source_snapshot_identity')
    return index


def wb_statistics(account):
    start = datetime.fromisoformat(os.environ.get('FBS_SCAN_START_UTC',
        '2026-11-01T21:00:00+00:00').replace('Z', '+00:00'))
    market_day = start.astimezone(ZoneInfo('Europe/Moscow')).date()
    response = requests.get('https://statistics-api.wildberries.ru/api/v1/supplier/orders',
        headers={'Authorization': os.environ['WB_TOKEN_' + account.upper()]},
        params={'dateFrom': market_day.isoformat() + 'T00:00:00', 'flag': 1}, timeout=(5, 40))
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError('wb_stats_schema')
    result = {}
    for row in rows:
        if row.get('srid'):
            result.setdefault(row['srid'], []).append(row)
    return result


def source(row, stats):
    number = row['number']
    if row['platform'] == 'yandex':
        business = os.environ['YANDEX_BUSINESS_ID_ACC1']
        response = requests.post(f'https://api.partner.market.yandex.ru/v1/businesses/{business}/orders',
            headers={'Api-Key': os.environ['YANDEX_API_KEY_ACC1']},
            json={'orderIds': [int(number)], 'programTypes': ['FBS'], 'fake': False},
            params={'limit': 20}, timeout=(5, 25))
        response.raise_for_status()
        rows = response.json().get('orders') or []
        if len(rows) != 1 or str(rows[0].get('orderId')) != number:
            raise RuntimeError('yandex_order_identity')
        order = rows[0]
        yandex_state = (order.get('status'), order.get('substatus'))
        if yandex_state not in (('PROCESSING', 'STARTED'), ('UNPAID', 'WAITING_USER_INPUT')):
            raise RuntimeError('yandex_status_changed')
        if (order.get('programType') != 'FBS' or order.get('fake') is not False
                or str(order.get('campaignId')) != str(row['campaign_id'])):
            raise RuntimeError('yandex_business_or_campaign_mismatch')
        lines = []
        for item in order.get('items') or []:
            quantity = item.get('count')
            if not item.get('offerId') or type(quantity) is not int or quantity <= 0:
                raise RuntimeError('yandex_item_identity')
            prices = item.get('prices') or {}
            if 'payment' not in prices:
                raise RuntimeError('yandex_payment_missing')
            money = Decimal(0)
            for key in ('payment', 'subsidy', 'cashback'):
                entry = prices.get(key)
                if entry is None:
                    continue
                if entry.get('currencyId') not in ('RUR', 'RUB'):
                    raise RuntimeError('yandex_non_rub')
                money += Decimal(str(entry['value'])) * 100
            if money <= 0 or money != money.to_integral_value():
                raise RuntimeError('yandex_price_invalid')
            if quantity != 1:
                # User confirmed on 63905348034 that payment+subsidy is the
                # full line total for count=2, not the unit amount.
                money = money / quantity
                if money != money.to_integral_value():
                    raise RuntimeError('yandex_line_total_not_divisible_to_kopeks')
            lines.append({'market_code': item['offerId'], 'quantity': quantity,
                          'price_kopeks': int(money), 'discount_percent': '0'})
        if not lines:
            raise RuntimeError('yandex_items_empty')
        return {'platform': 'yandex', 'account': 'acc1', 'number': number,
                'created_at': order['creationDate'],
                'status': [order['status'], order['substatus']], 'campaign_id': order['campaignId'],
                'price_source': 'Yandex API item total / count',
                'lines': lines}
    if row['platform'] == 'ozon':
        account = row['account'].removeprefix('oz_')
        suffix = account.upper()
        h = {'Client-Id': os.environ['OZON_CLIENT_ID_' + suffix],
             'Api-Key': os.environ['OZON_API_KEY_' + suffix]}
        response = requests.post('https://api-seller.ozon.ru/v3/posting/fbs/get',
            headers=h, json={'posting_number': number, 'with': {'financial_data': True}},
            timeout=(5, 25))
        response.raise_for_status()
        p = response.json().get('result')
        if not isinstance(p, dict) or p.get('posting_number') != number:
            raise RuntimeError('ozon_identity')
        if p.get('status') != 'awaiting_packaging':
            raise RuntimeError('ozon_status_changed')
        products = p.get('products') or []
        finance = (p.get('financial_data') or {}).get('products') or []
        if not products or len(products) != len(finance):
            raise RuntimeError('ozon_financial_positions_incomplete')
        by_sku = {str(f.get('product_id')): f for f in finance}
        if len(by_sku) != len(finance):
            raise RuntimeError('ozon_financial_sku_ambiguous')
        lines = []
        for product in products:
            f = by_sku.get(str(product.get('sku')))
            if f is None or not product.get('offer_id') or type(product.get('quantity')) is not int:
                raise RuntimeError('ozon_product_finance_mismatch')
            old = Decimal(str(f.get('old_price')))
            discount = Decimal(str(f.get('total_discount_percent') or 0))
            if old <= 0 or old * 100 != (old * 100).to_integral_value() or not 0 <= discount < 100:
                raise RuntimeError('ozon_invalid_amount')
            lines.append({'market_code': product['offer_id'], 'quantity': product['quantity'],
                          'price_kopeks': int(old * 100), 'discount_percent': str(discount)})
        stamp = p.get('in_process_at') or p.get('created_at')
        if not stamp:
            raise RuntimeError('ozon_created_at_missing')
        return {'platform': 'ozon', 'account': account, 'number': number,
                'created_at': stamp, 'status': p['status'], 'lines': lines}
    if row['platform'] != 'wb' or row['account'] not in ('acc1', 'acc2', 'wb_acc1', 'wb_acc2'):
        raise RuntimeError('scope_account_mismatch')
    account = row['account'].removeprefix('wb_')
    h = {'Authorization': os.environ['WB_TOKEN_' + account.upper()]}
    response = requests.post('https://marketplace-api.wildberries.ru/api/v3/orders/status',
        headers=h, json={'orders': [int(number)]}, timeout=(5, 25))
    response.raise_for_status()
    states = response.json().get('orders') or []
    if len(states) != 1 or states[0].get('id') != int(number):
        raise RuntimeError('wb_status_identity')
    if (states[0].get('supplierStatus'), states[0].get('wbStatus')) != ('new', 'waiting'):
        raise RuntimeError('wb_status_changed')
    hits = stats[account].get(row['rid'], [])
    if len(hits) == 1 and hits[0].get('isCancel') is False and hits[0].get('priceWithDisc') is not None:
        money = Decimal(str(hits[0]['priceWithDisc'])) * 100
        origin = 'WB statistics.priceWithDisc'
    elif not hits and number == '6178120612':
        # User confirmed 2706.56 RUB for this exact WB order in this session.
        money = Decimal(270656)
        origin = 'user-confirmed RUB amount 2026-11-02'
    elif not hits and number in ('6179632720', '6179632722'):
        # User confirmed 7208.03 RUB for each BYN-denominated order.
        money = Decimal(720803)
        origin = 'user-confirmed RUB amount 2026-11-02 (WB BYN order)'
    elif not hits and row.get('currency_code') == 643:
        money = Decimal(str(row['buyer_price_kopeks']))
        origin = 'WB Orders.price fallback'
    else:
        raise RuntimeError('wb_price_ambiguous')
    if money <= 0 or money != money.to_integral_value():
        raise RuntimeError('wb_price_invalid')
    return {'platform': 'wb', 'account': account, 'number': number,
            'created_at': row['created_at'], 'status': ['new', 'waiting'],
            'rid': row['rid'], 'price_source': origin,
            'lines': [{'market_code': row['article'], 'quantity': 1,
                       'price_kopeks': int(money), 'discount_percent': '0'}]}


def inspect(row, stats, prices, digest, promised=None):
    market = source(row, stats)
    account = market['account']
    # Per-order allocations are local: two pre-POST inspections must not mutate
    # the batch promises. Selected units also cannot be reused by another line.
    promised = defaultdict(Decimal, promised or {})
    def remember(items):
        for selected_item in items:
            store = selected_item.get('stock_store_id')
            if store:
                promised[(store, selected_item['card_id'])] += Decimal(selected_item['quantity'])
    if find_customerorder(row['number']):
        raise RuntimeError('already_in_ms')
    selected = []
    lines = []
    for item in market['lines']:
        code = base_code(item['market_code'])
        cards = product_cards(code)
        try:
            composition = set_components(code)
        except Exception:
            composition = None
        is_set = bool(composition) or any(re.search(r'\b(?:набор\w*|комплект\w*)\b',
                                                  c.get('name') or '', re.I) for c in cards)
        if is_set:
            whole = _ready_set_lines(item, code, account, cards, prices, promised)
            if whole is not None:
                whole_lines, whole_proof = whole
                lines.extend(whole_lines)
                selected.extend(whole_proof)
                remember(whole_proof)
                continue
        manual_parent = False
        if composition:
            if (os.getenv('FBS_SET_SPLIT_ENABLED') != '1' or
                    not _confirmed_set_offer(item['market_code'], code, cards,
                                             account=account)):
                manual_parent = True
            else:
                try:
                    kit_lines, kit_proof = _kit_lines(item, code, account, prices, composition, promised)
                except (SelectionError, ValueError):
                    manual_parent = True
                else:
                    lines.extend(kit_lines)
                    selected.extend(kit_proof)
                    remember(kit_proof)
                    continue
        if not cards:
            raise RuntimeError('exact_base_product_card_missing:' + code)
        if composition is None and any(re.search(r'\b(?:набор\w*|комплект\w*)\b',
                                               c.get('name') or '', re.I) for c in cards):
            manual_parent = True
        stock = _stock(cards, STORE[account])
        domain = [Card(c['id'], c['externalCode'], ms_api.meta_id(c, 'supplier') or '',
                       (c.get('code') or '').removeprefix(code).lower(), prices.get(c['id']),
                       Decimal(str(stock[c['id']]['stock'])), Decimal(str(stock[c['id']]['reserve'])),
                       c.get('archived', False)) for c in cards]
        domain = [Card(c.id, c.external_code, c.supplier_id, c.brand, c.purchase_kopeks,
                       c.stock, c.reserve + promised.get((STORE[account], c.id), 0), c.archived)
                  for c in domain]
        remote_stock, remote_domain = {}, []
        required = Decimal(item['quantity'])
        if not manual_parent and not any(c.available >= required and priority(c) is not None for c in domain):
            remote_stock = _stock(cards, STORE_REMOTE)
            remote_domain = [Card(c.id, c.external_code, c.supplier_id, c.brand, c.purchase_kopeks,
                Decimal(str(remote_stock[c.id]['stock'])),
                Decimal(str(remote_stock[c.id]['reserve'])) + promised.get((STORE_REMOTE, c.id), 0),
                c.archived) for c in domain]
        remote_by_id = {c.id: c for c in remote_domain}
        candidate_evidence = [
            {'id': c.id, 'code': next(x.get('code') for x in cards if x['id'] == c.id),
             'stock': stock[c.id], 'available': str(c.available),
             'tier': tier(c), 'priority': priority(c),
             'purchase_kopeks': c.purchase_kopeks,
             'supplier_available': str(remote_by_id[c.id].available) if c.id in remote_by_id else None}
            for c in domain
        ]
        manual_fallback = False
        try:
            chosen, available = choose_card(domain, code, Decimal(item['quantity']), remote_domain)
        except SelectionError as exc:
            # An existing msk/dsk parent is a confirmed same-code product for a
            # set whose composition is unavailable; leave it for human review.
            if str(exc) != 'current_purchase_price_missing' and not manual_parent:
                raise
            suffix = 'msk' if account == 'acc1' else 'dsk'
            parents = [c for c in cards if c.get('code') == code + suffix]
            fallback = parents[0] if len(parents) == 1 else cards[0] if len(cards) == 1 else None
            if fallback is None:
                # The user explicitly permits an existing same-code supplier
                # variant for unresolved goods, with manual confirmation status.
                ranked = sorted((c for c in cards if tier(next(d for d in domain if d.id == c['id'])) is not None),
                                key=lambda c: (tier(next(d for d in domain if d.id == c['id'])),
                                               c.get('code') or '', c['id']))
                fallback = ranked[0] if ranked else None
            if fallback is None and manual_parent:
                fallback = min(cards, key=lambda c: (c.get('code') or '', c['id']))
            if fallback is None:
                raise
            chosen = next(c for c in domain if c.id == fallback['id'])
            available = False
            manual_fallback = True
        if manual_parent:
            suffix = 'msk' if account == 'acc1' else 'dsk'
            parent = [c for c in cards if c.get('code') == code + suffix]
            if len(parent) == 1:
                chosen = next(c for c in domain if c.id == parent[0]['id'])
            available = False
            manual_fallback = True
        selected.append({'market_code': item['market_code'], 'base_code': code,
                         'card_id': chosen.id, 'card_code': next(c.get('code') for c in cards if c['id'] == chosen.id),
                         'quantity': str(item['quantity']),
                         'stock': stock[chosen.id], 'available': available,
                         'tier': tier(chosen), 'priority': priority(chosen),
                         'supplier_id': chosen.supplier_id, 'selection_policy': POLICY_VERSION,
                         'supplier_available': str(remote_by_id[chosen.id].available) if chosen.id in remote_by_id else None,
                         'supplier_stock': remote_stock.get(chosen.id),
                         'purchase_kopeks': chosen.purchase_kopeks,
                         'manual_fallback': manual_fallback,
                         'manual_parent': manual_parent,
                         'stock_store_id': STORE[account] if available else (STORE_REMOTE if not manual_fallback else None),
                         'candidate_evidence': candidate_evidence})
        lines.append(Line(chosen.id, Decimal(item['quantity']), item['price_kopeks'],
                          Decimal(item['discount_percent'])))
        remember([selected[-1]])
    total = sum(line_total(x) for x in lines)
    payload = build_payload(platform=market['platform'], account=account,
        external_id=row['number'], lines=lines, expected_kopeks=total, available=False,
        now=datetime.now(timezone.utc),
        market_created_at=datetime.fromisoformat(market['created_at'].replace('Z', '+00:00')),
        payment_pending=market['platform'] == 'yandex' and market['status'] == ['UNPAID', 'WAITING_USER_INPUT'])
    return {'market': market, 'selected': selected, 'total_kopeks': total,
            'prices_digest': digest, 'payload': payload}


def create_one(row, stats, prices, digest, promised=None):
    number = row['number']
    folder = OUT / ('next5_' + number)
    if (folder / 'intent.json').exists():
        raise RuntimeError('prior_intent_requires_reconciliation')
    first = inspect(row, stats, prices, digest, promised)
    save(folder / 'preflight.json', first)
    second = inspect(row, stats, prices, digest, promised)
    if any(first[key] != second[key] for key in ('market', 'selected', 'total_kopeks', 'prices_digest')):
        raise RuntimeError('source_or_goods_changed_before_post')
    if any(not x['available'] and not x.get('manual_fallback') for x in second['selected']):
        from ops.fbs_ms_import_scheduler import enter_digest
        if enter_digest() != digest:
            raise RuntimeError('set_purchase_receipt_changed_before_post')
    save(folder / 'intent.json', {'at': datetime.now(timezone.utc).isoformat(),
                                 'key': [row['platform'], row['account'], number],
                                 'payload': second['payload']}, exclusive=True)
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
    if response.status_code not in (200, 201):
        raise RuntimeError('ms_post_rejected')
    created = response.json()
    order = ms_api.get('/entity/customerorder/' + created['id'],
                       {'expand': 'state,store,organization,agent,positions.assortment'})
    save(folder / 'readback.json', order)
    expected_state = ('Ожидает оплаты' if second['market']['platform'] == 'yandex'
                      and second['market']['status'] == ['UNPAID', 'WAITING_USER_INPUT']
                      else 'Ожидает подтверждения')
    if (order.get('name') != number or order.get('sum') != second['total_kopeks']
            or order.get('state', {}).get('name') != expected_state
            or ms_api.meta_id(order, 'store') != STORE[second['market']['account']]
            or _quantities(order.get('positions', {}).get('rows', [])) != _quantities(second['payload']['positions'])
            or any(p.get('vat') != 5 for p in order.get('positions', {}).get('rows', []))):
        raise RuntimeError('ms_readback_mismatch_stop_batch')
    if promised is not None:
        # MS may already have reserved the new order at readback. Keep only
        # units not yet reflected in that own reserve; remote units are never
        # reserved by a customerorder on our warehouse.
        new_promises = _unreserved_promises(second['selected'], order.get('positions', {}).get('rows', []),
                                            STORE[second['market']['account']])
        for key, quantity in new_promises.items():
            promised[key] += quantity
    return {'number': number, 'result': 'created', 'platform': row['platform'],
            'account': row['account'], 'sum_kopeks': order['sum'],
            'ms_status': order['state']['name'],
            'card_codes': [x['card_code'] for x in second['selected']],
            'price_source': second['market'].get('price_source'),
            'url': order['meta'].get('uuidHref')}


def run():
    if not 1 <= len(SCOPE) <= 5:
        raise RuntimeError('FBS_IMPORT_SCOPE must list 1–5 exact order numbers')
    index = source_index()
    snapshot = json.loads((OUT / 'prices_snapshot_all.json').read_text())
    wb_accounts = {index[n]['account'].removeprefix('wb_') for n in SCOPE
                   if index[n]['platform'] == 'wb'}
    stats = {account: (wb_statistics(account) if any(index[n]['platform'] == 'wb'
                    and index[n]['account'].removeprefix('wb_') == account
                    and n not in USER_PRICE_ORDERS for n in SCOPE) else {})
             for account in sorted(wb_accounts)}
    results = []
    for number in SCOPE:
        row = index[number]
        try:
            if find_customerorder(number):
                result = {'number': number, 'result': 'already_in_ms'}
            else:
                result = create_one(row, stats, snapshot['prices'], snapshot['digest'])
        except Exception as exc:
            result = {'number': number, 'result': 'stopped', 'reason': type(exc).__name__ + ':' + str(exc)[:180]}
            results.append(result)
            save(OUT / 'next5_result.json', {'results': results})
            print(json.dumps(result, ensure_ascii=False), flush=True)
            break
        results.append(result)
        save(OUT / 'next5_result.json', {'results': results})
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        parser.error('explicit --apply required')
    run()

# поток: inv
"""Three explicitly authorized MSK set-card imports from batch10 exclusions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os

from core import ms_api
from ops.fbs_ms_import_batch10 import market_source, base_code, stock_for
from ops.fbs_ms_import_pilot import save
from ops.fbs_ms_import_policy import AllocationPool, Card
from ops.fbs_ms_import_read import find_customerorder, product_cards
from ops.fbs_ms_order_payload import Line, STORE, build_payload, line_total
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/fbs_ms_recovery_20261102/msk_sets'
SCOPE = (('wb', 'acc1', '6176190006'), ('wb', 'acc1', '6176699856'),
         ('ozon', 'acc1', '93364143-0132-1'))
SET_BASES = {'6624', '1222', '6418'}
assert len(SCOPE) == 3


def inspect(platform: str, account: str, number: str) -> dict:
    source = market_source(platform, number)
    existing = find_customerorder(number)
    if existing:
        raise RuntimeError('already_in_ms:' + existing['id'])
    chosen = []; pool = AllocationPool()
    for item in source['lines']:
        base = base_code(item['market_code'])
        cards = product_cards(base)
        if base in SET_BASES:
            matches = [c for c in cards if c.get('code') == base + 'msk']
            if len(matches) != 1:
                raise RuntimeError('msk_set_card_missing_or_ambiguous:' + base)
            card = matches[0]
            proof = {'market_code': item['market_code'], 'base_code': base,
                     'card_id': card['id'], 'card_code': card['code'],
                     'reason': 'user_confirmed_msk_set_card'}
        else:
            stock = stock_for(cards, STORE[account])
            domain = [Card(c['id'], c['externalCode'], ms_api.meta_id(c, 'supplier') or '',
                           (c.get('code') or '').removeprefix(base).lower(), None,
                           Decimal(str(stock[c['id']]['stock'])), Decimal(str(stock[c['id']]['reserve'])),
                           c.get('archived', False)) for c in cards]
            selected, available = pool.choose(domain, base, Decimal(item['quantity']))
            if not available:
                raise RuntimeError('other_component_not_available:' + base)
            card = next(c for c in cards if c['id'] == selected.id)
            proof = {'market_code': item['market_code'], 'base_code': base,
                     'card_id': card['id'], 'card_code': card['code'],
                     'stock': stock[card['id']], 'available': True}
        line = Line(card['id'], Decimal(item['quantity']), item['price_kopeks'],
                    Decimal(item['discount_percent']))
        chosen.append((line, proof))
    if not any(p['base_code'] in SET_BASES for _, p in chosen):
        raise RuntimeError('no_confirmed_msk_set_in_order')
    total = sum(line_total(line) for line, _ in chosen)
    stamp = datetime.fromisoformat(source['created_at'].replace('Z', '+00:00'))
    payload = build_payload(platform=platform, account=account, external_id=number,
        lines=[line for line, _ in chosen], expected_kopeks=total, available=False,
        now=datetime.now(timezone.utc), market_created_at=stamp)
    return {'source': source, 'selected': [p for _, p in chosen],
            'total_kopeks': total, 'payload': payload}


def create_one(platform: str, account: str, number: str) -> dict:
    folder = OUT / (platform + '_' + account + '_' + number)
    if (folder / 'intent.json').exists():
        raise RuntimeError('prior_intent_requires_reconciliation')
    first = inspect(platform, account, number)
    save(folder / 'preflight.json', first)
    second = inspect(platform, account, number)
    if first['source'] != second['source'] or first['selected'] != second['selected']:
        raise RuntimeError('source_or_selection_changed_before_post')
    save(folder / 'intent.json', {'at': datetime.now(timezone.utc).isoformat(),
                                 'key': [platform, account, number], 'payload': second['payload']},
         exclusive=True)
    try:
        import requests
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
    actual = [(x['assortment']['id'], Decimal(str(x['quantity'])), x.get('vat'))
              for x in order.get('positions', {}).get('rows', [])]
    expected = [(x['card_id'], Decimal(str(item['quantity'])), 5)
                for x, item in zip(second['selected'], second['source']['lines'])]
    if (order['name'] != number or order['sum'] != second['total_kopeks']
            or order['state']['name'] != 'Ожидает подтверждения'
            or ms_api.meta_id(order, 'store') != STORE[account]
            or actual != expected or order.get('demands')
            or order.get('deliveryPlannedMoment')):
        raise RuntimeError('ms_readback_mismatch_stop')
    return {'order': number, 'platform': platform, 'account': account,
            'ms_id': created['id'], 'sum_kopeks': order['sum'],
            'status': order['state']['name'], 'url': order['meta'].get('uuidHref'),
            'products': [x['card_code'] for x in second['selected']]}


def run(apply: bool):
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700); OUT.chmod(0o700)
    results = []
    for platform, account, number in SCOPE:
        if apply:
            result = create_one(platform, account, number)
        else:
            check = inspect(platform, account, number)
            result = {'order': number, 'platform': platform, 'account': account,
                      'sum_kopeks': check['total_kopeks'],
                      'products': [x['card_code'] for x in check['selected']]}
        results.append(result); save(OUT / 'run.json', {'results': results, 'complete': False})
        print(json.dumps(result, ensure_ascii=False), flush=True)
    save(OUT / 'run.json', {'results': results, 'complete': True})
    return {'scope': len(SCOPE), 'completed': len(results), 'apply': apply}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.apply), ensure_ascii=False))

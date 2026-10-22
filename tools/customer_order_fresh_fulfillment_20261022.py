# поток: inv
"""Fresh pending orders: same-code supplier substitutions, TC bundles, audited fulfillment."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import sys

from tools import customer_order_batch17_20261022 as b
from tools.customer_order_stock_plan_20261022 import Planner, expanded_positions, line_sum, line_vat, brand, supplier


def collect():
    """Read-only collection into a new directory; never replay a historical wave."""
    from tools import customer_order_remaining_bundles_20261022 as bundles
    assert not (b.OUT / 'scope_orders.json').exists(), 'Use a new wave directory'
    b.OUT.mkdir(parents=True, exist_ok=True)
    b.OUT.chmod(0o700)
    metadata = b.ms_api.get('/entity/customerorder/metadata')
    b.dump(b.OUT / 'metadata.json', metadata)
    states = [s for s in metadata['states'] if s['name'] in ('Ожидает обработки', 'Ожидает подтверждения')]
    assert states, 'Waiting status not found'
    orders = []
    for state in states:
        offset = 0
        while True:
            raw = b.ms_api.get('/entity/customerorder', {'limit': 20, 'offset': offset,
                'filter': 'state=' + state['meta']['href'], 'order': 'moment,asc', **b.EXPAND})
            b.dump(b.OUT / f'pending_{state["id"]}_{offset}.json', raw)
            orders.extend(raw['rows']); offset += len(raw['rows'])
            if offset >= raw['meta']['size']:
                break
    b.dump(b.OUT / 'scope_orders.json', orders)
    today = datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d')
    b.dump(b.OUT / 'wave_config.json', {'planned_date': today, 'captured_at': b.now().isoformat()})
    bundles.OUT = b.OUT
    codes = sorted({r['assortment']['externalCode'] for o in orders for r in o['positions']['rows']})
    boms, sets, errors = {}, [], {}
    for code in codes:
        try:
            bom, is_set = bundles.composition(code)
            boms[code] = dict(bom)
            if is_set:
                sets.append(code)
        except Exception as exc:
            errors[code] = str(exc)[:200]
    b.dump(b.OUT / 'compositions.json', {'boms': boms, 'set_codes': sets, 'errors': errors})
    wanted = sorted(set(codes) | {c for bom in boms.values() for c in bom})
    cards = {}
    for start in range(0, len(wanted), 10):
        offset = 0
        while True:
            raw = b.ms_api.get('/entity/assortment', {'limit': 100, 'offset': offset,
                'filter': ['externalCode=' + c for c in wanted[start:start + 10]] + ['archived=false'], 'expand': 'supplier'})
            b.dump(b.OUT / f'candidates_{start}_{offset}.json', raw)
            for card in raw['rows']:
                if card['meta']['type'] == 'product':
                    cards[card['id']] = card
            offset += len(raw['rows'])
            if offset >= raw['meta']['size']:
                break
    b.dump(b.OUT / 'candidate_cards.json', cards)
    print('Read-only collection:', len(orders), 'orders;', len(cards), 'products;', len(errors), 'TC errors')


def stock_for(o, cards, folder):
    stock = {}
    ids = sorted(cards)
    for start in range(0, len(ids), 15):
        chunk = ids[start:start + 15]
        raw = b.ms_api.get('/report/stock/all', {'limit': 100, 'filter':
            ['store=' + o['store']['meta']['href'], 'stockMode=all'] + ['product=' + cards[pid]['meta']['href'] for pid in chunk]})
        b.dump(folder / f'stock_candidates_{start}.json', raw)
        assert raw['meta']['size'] == len(raw['rows'])
        for pid in chunk:
            stock[o['store']['id'] + '/' + pid] = {'stock': 0, 'reserve': 0}
        for row in raw['rows']:
            pid = row['meta']['href'].split('?')[0].rsplit('/', 1)[-1]
            assert pid in chunk
            stock[o['store']['id'] + '/' + pid] = row
    return stock


def apply():
    assert not (b.OUT / 'status_result.json').exists(), 'Stage already completed'
    originals = b.read(b.OUT / 'scope_orders.json')
    assert len(originals) < 25, 'Larger waves need bulk-change procedure'
    comp = b.read(b.OUT / 'compositions.json')
    allcards = b.read(b.OUT / 'candidate_cards.json')
    state = next(s for s in b.read(b.OUT / 'metadata.json')['states'] if s['name'] == 'Собирается')
    result = {}
    for snapshot in originals:
        cid = snapshot['id']; folder = b.OUT / cid
        entry = {'name': snapshot['name'], 'verified': False}
        try:
            o = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
            b.dump(folder / 'order_before.json', o)
            assert o['state']['name'] in ('Ожидает обработки', 'Ожидает подтверждения'), 'Order advanced to another state'
            assert o.get('applicable') and not o.get('demands') and o.get('store')
            assert len(o['positions']['rows']) == o['positions']['meta']['size']
            assert not any(r.get('pack') for r in o['positions']['rows'])
            codes = {r['assortment']['externalCode'] for r in o['positions']['rows']}
            assert codes <= comp['boms'].keys(), 'Unconfirmed TC composition'
            wanted = codes | {c for code in codes for c in comp['boms'][code]}
            cards = {pid: c for pid, c in allcards.items() if c['externalCode'] in wanted}
            for row in o['positions']['rows']:
                cards.setdefault(row['assortment']['id'], row['assortment'])
            stock = stock_for(o, cards, folder)
            planner = Planner(cards, stock, comp['boms'], set_codes=set(comp['set_codes']), allow_single_replacements=True)
            plan = planner.order(o, planner.initial, {})
            if not plan['ready']:
                entry.update(action='shortage', reason=plan.get('reason'), shortage=plan.get('shortage', []))
                result[cid] = entry; b.dump(b.OUT / 'apply_progress.json', result)
                continue
            positions = [p for line in plan['lines'] for p in expanded_positions(line, o, cards)]
            # Reserve the selected stock immediately so subsequent orders cannot promise it again.
            for p in positions:
                p['reserve'] = p['quantity']
            assert sum(line_sum(p, o) for p in positions) == Decimal(str(o['sum'])), 'Amount mismatch'
            assert sum(line_vat(p, o) for p in positions) == Decimal(str(o.get('vatSum', 0))), 'VAT mismatch'
            expected = Counter()
            for r in o['positions']['rows']:
                for code, qty in comp['boms'][r['assortment']['externalCode']].items():
                    expected[code] += Decimal(str(qty)) * Decimal(str(r['quantity']))
            actual = Counter()
            for p in positions:
                card = cards[b.ms_api.meta_id(p, 'assortment')]
                actual[card['externalCode']] += Decimal(str(p['quantity']))
            assert actual == expected, 'Selected external codes do not match the order composition'
            selected = {}
            for pid in {b.ms_api.meta_id(p, 'assortment') for p in positions}:
                live = b.ms_api.get(cards[pid]['meta']['href'], {'expand': 'supplier'})
                assert not live.get('archived') and live['externalCode'] == cards[pid]['externalCode']
                assert 'брак' not in (live.get('name', '') + ' ' + live.get('pathName', '')).casefold()
                selected[pid] = live
            b.dump(folder / 'selected_cards.json', selected)
            b.dump(folder / 'plan.json', {'modes': [x['mode'] for x in plan['lines']], 'expected_codes': expected})
            latest = b.ms_api.get('/entity/customerorder/' + cid)
            assert latest['updated'] == o['updated'] and not latest.get('demands'), 'Order changed during planning'
            payload = {'state': {'meta': state['meta']}, 'deliveryPlannedMoment': b.DATE}
            position_change = b.signature(positions) != b.signature(o['positions']['rows'])
            reservation_change = any(Decimal(str(r.get('reserve', 0))) != Decimal(str(r['quantity'])) for r in o['positions']['rows'])
            if position_change or reservation_change:
                payload['positions'] = positions
            b.one_write(folder, 'fulfill', 'PUT', '/entity/customerorder/' + cid, payload)
            after = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
            b.dump(folder / 'order_after.json', after)
            assert b.signature(after['positions']['rows']) == b.signature(positions)
            assert after['sum'] == o['sum'] and after.get('vatSum') == o.get('vatSum')
            for key in ('store', 'organization', 'agent'):
                assert b.ms_api.meta_id(after, key) == b.ms_api.meta_id(o, key)
            assert after['state']['name'] == 'Собирается' and after.get('deliveryPlannedMoment', '').startswith(b.DATE)
            b.stock_check(after, folder / 'after_allocation')
            entry.update(verified=True, action='ready', changed_positions=position_change,
                         modes=[x['mode'] for x in plan['lines']])
        except Exception as exc:
            entry.update(action='review', error=str(exc)[:300], write_attempted=(folder / 'fulfill_intent.json').exists())
        result[cid] = entry
        b.dump(b.OUT / 'apply_progress.json', result)
    end = b.now()
    b.dump(b.OUT / 'apply_result.json', {'orders': result, 'finished_at': end.isoformat()})
    b.dump(b.OUT / 'status_result.json', {'orders': {cid: v for cid, v in result.items() if v['verified']},
        'finished_at': end.isoformat(), 'shipment_check_not_before': (end + timedelta(minutes=5)).isoformat()})
    print('Results:', dict(Counter(v['action'] for v in result.values())))
    for entry in result.values():
        print(entry['name'], entry['action'], entry.get('modes', entry.get('reason', entry.get('error'))))
    print('Check shipments not before', (end + timedelta(minutes=5)).isoformat())


if __name__ == '__main__':
    b.OUT = Path(sys.argv[2])
    config = b.OUT / 'wave_config.json'
    if config.exists():
        planned = b.read(config)['planned_date']
        datetime.strptime(planned, '%Y-%m-%d')
        if sys.argv[1] == 'apply':
            assert planned == datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d'), 'Collect a fresh wave for today'
        b.DATE = planned + ' 00:00:00'
    elif sys.argv[1] in ('apply', 'shipments'):
        assert datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d') == '2026-10-22', 'Historical wave: do not replay'
    if sys.argv[1] == 'collect':
        collect()
    elif sys.argv[1] == 'apply':
        apply()
    elif sys.argv[1] == 'shipments':
        check = b.demand_check
        b.demand_check = lambda d, o: check(d, o) and d.get('vatSum') == o.get('vatSum')
        b.shipments()
    else:
        raise ValueError('Unknown stage')

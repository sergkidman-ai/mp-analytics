# поток: inv
"""Authorized bundle decomposition only; retain every order header and monetary total."""
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

import requests

from core import ms_api
from tools.customer_order_batch17_20261022 import ROOT, dump, read, one_write, signature
from tools.customer_order_stock_plan_20261022 import Planner, expanded_positions, line_sum, brand, supplier

OUT = ROOT / 'remaining_bundles'


def composition(code, path=()):
    assert code and code not in path and len(path) < 12, 'TC composition cycle or invalid code'
    file = OUT / 'tc' / (code + '.json')
    if file.exists(): obj = read(file)
    else:
        for attempt in range(3):
            r = requests.post('https://thecartridge.ru/api/catalog/mix_data',
                headers={'Api-Key': os.environ['CARTRIDGE_API_KEY'], 'Content-Type': 'application/json'},
                json={'external_code': code}, timeout=40)
            if r.status_code != 429 or attempt == 2:
                break
            try: pause = max(15, float(r.headers.get('Retry-After', '15')))
            except ValueError: pause = 30
            time.sleep(min(pause, 60))
        assert r.status_code == 200, 'TC HTTP ' + str(r.status_code)
        obj = r.json(); dump(file, obj); time.sleep(1.1)
    if isinstance(obj, dict) and obj.get('error') == 'not_mix':
        return Counter({code: 1}), False
    assert isinstance(obj, list) and obj, 'TC did not confirm code ' + code
    result = Counter()
    for item in obj:
        if isinstance(item, dict):
            assert not set(item) - {'external_code', 'incoming_references', 'outgoing_references'}, 'Unknown TC component quantity format'
            child = str(item['external_code'])
        else:
            assert isinstance(item, str), 'Unknown TC component format'
            child = item
        result.update(composition(child, path + (code,))[0])
    return result, True


def prepare():
    assert not (OUT / 'prepared.json').exists(), 'Preparation already frozen'
    orders = read(OUT / 'scope_orders.json')
    codes = sorted({r['assortment'].get('externalCode', '') for o in orders for r in o['positions']['rows']})
    boms, sets, errors = {}, [], {}
    for code in codes:
        try:
            b, is_set = composition(code); boms[code] = dict(b)
            if is_set: sets.append(code)
        except Exception as exc: errors[code] = str(exc)[:200]
    dump(OUT / 'compositions.json', {'boms': boms, 'set_codes': sets, 'errors': errors})
    target = [o for o in orders if any(r['assortment'].get('externalCode') in sets for r in o['positions']['rows'])]
    wanted = sorted({c for o in target for r in o['positions']['rows'] if r['assortment'].get('externalCode') in boms
                     for c in [r['assortment']['externalCode'], *boms[r['assortment']['externalCode']]]})
    cards = {}
    for start in range(0, len(wanted), 10):
        chunk = wanted[start:start + 10]; rows, offset = [], 0
        while True:
            r = ms_api.get('/entity/assortment', {'limit': 100, 'offset': offset,
                'filter': ['externalCode=' + c for c in chunk] + ['archived=false'], 'expand': 'supplier'})
            rows += r['rows']; offset += len(r['rows'])
            if offset >= r['meta']['size']: break
        for card in rows:
            assert card['externalCode'] in chunk
            if card['meta']['type'] == 'product': cards[card['id']] = card
    dump(OUT / 'candidate_cards.json', cards)
    dump(OUT / 'prepared.json', {'orders_scanned': len(orders), 'raw_bundle_orders': len(target),
        'source_codes': len(codes), 'confirmed_set_codes': len(sets), 'candidate_cards': len(cards),
        'tc_errors': errors, 'prepared_at': datetime.now(timezone.utc).isoformat()})
    print('Scanned:', len(orders), 'raw bundle orders:', len(target), 'candidate cards:', len(cards), 'TC errors:', len(errors), flush=True)


def apply():
    assert not (OUT / 'result.json').exists(), 'This wave already finished'
    orders = sorted(read(OUT / 'scope_orders.json'), key=lambda o: (o['moment'], o['id']))
    comp = read(OUT / 'compositions.json'); boms, sets = comp['boms'], set(comp['set_codes'])
    allcards = read(OUT / 'candidate_cards.json'); result = {}; changed = 0
    for snapshot in orders:
        cid = snapshot['id']; folder = OUT / cid
        try:
            o = ms_api.get('/entity/customerorder/' + cid, {'expand': 'state,store,positions.assortment'})
            dump(folder / 'before.json', o)
            source_codes = [r['assortment'].get('externalCode') for r in o['positions']['rows']]
            if any(c not in boms for c in source_codes):
                raise ValueError('Состав изменился либо код не подтверждён в ТК')
            if not any(c in sets for c in source_codes):
                result[cid] = {'name': o['name'], 'action': 'already_components', 'state': o['state']['name'], 'changed': False}
                dump(OUT / 'progress.json', result); continue
            assert not o.get('demands'), 'Есть связанная отгрузка — позиции не меняем'
            assert o.get('applicable') and o['state']['name'] in {'Ожидает подтверждения', 'Подтвержден', 'Собирается', 'Неукомплектован'}, 'Заказ уже в другом этапе'
            assert o.get('store') and len(o['positions']['rows']) == o['positions']['meta']['size']
            assert not any(r.get('pack') for r in o['positions']['rows']), 'Позиции в упаковках требуют проверки'
            need_codes = {code for c in source_codes for code in [c, *boms[c]]}
            cards = {pid: c for pid, c in allcards.items() if c['externalCode'] in need_codes}
            for row in o['positions']['rows']: cards[row['assortment']['id']] = row['assortment']
            ids = sorted(cards); sid = o['store']['id']; stock = {}
            for start in range(0, len(ids), 15):
                chunk = ids[start:start + 15]
                r = ms_api.get('/report/stock/all', {'limit': 100, 'filter': ['store=' + o['store']['meta']['href'], 'stockMode=all']
                    + ['product=' + cards[pid]['meta']['href'] for pid in chunk]})
                assert r['meta']['size'] == len(r['rows'])
                for pid in chunk: stock[sid + '/' + pid] = {'stock': 0, 'reserve': 0}
                for row in r['rows']:
                    pid = row['meta']['href'].split('?')[0].rsplit('/', 1)[-1]
                    assert pid in chunk
                    stock[sid + '/' + pid] = row
            dump(folder / 'stock_before.json', stock)
            planner = Planner(cards, stock, boms, set_codes=sets, allow_single_replacements=False)
            plan = planner.order(o, planner.initial, {})
            if not plan['ready']:
                result[cid] = {'name': o['name'], 'action': 'shortage', 'changed': False,
                    'reason': plan.get('reason'), 'shortage': plan.get('shortage', [])}
                dump(OUT / 'progress.json', result); print('Hold:', o['name'], plan.get('reason'), flush=True); continue
            positions = [p for line in plan['lines'] for p in expanded_positions(line, o, cards)]
            assert sum(line_sum(p, o) for p in positions) == Decimal(str(o['sum'])), 'Monetary rounding mismatch'
            selected = {ms_api.meta_id(p, 'assortment') for p in positions}
            for pid in selected:
                live = ms_api.get(cards[pid]['meta']['href'], {'expand': 'supplier'})
                assert not live.get('archived') and live['externalCode'] == cards[pid]['externalCode']
                assert brand(live)[0] == brand(cards[pid])[0] and supplier(live)[0] == supplier(cards[pid])[0]
                cards[pid] = live
            dump(folder / 'selected_cards.json', {pid: cards[pid] for pid in selected})
            dump(folder / 'payload.json', {'positions': positions})
            latest = ms_api.get('/entity/customerorder/' + cid)
            assert latest['updated'] == o['updated'] and not latest.get('demands'), 'Заказ изменился во время подбора'
            assert changed < 50, 'Exceeds the frozen single-wave entity limit'
            response = one_write(folder, 'positions', 'PUT', '/entity/customerorder/' + cid, {'positions': positions})
            after = ms_api.get('/entity/customerorder/' + cid, {'expand': 'state,store,positions.assortment'})
            dump(folder / 'after.json', after)
            assert signature(after['positions']['rows']) == signature(positions), 'Readback positions mismatch'
            assert after['sum'] == o['sum'] and ms_api.meta_id(after, 'store') == sid
            assert after['state']['name'] == o['state']['name'] and after.get('deliveryPlannedMoment') == o.get('deliveryPlannedMoment')
            assert ms_api.meta_id(after, 'organization') == ms_api.meta_id(o, 'organization')
            assert ms_api.meta_id(after, 'agent') == ms_api.meta_id(o, 'agent')
            changed += 1
            result[cid] = {'name': o['name'], 'action': 'decomposed', 'changed': True, 'verified': True,
                'before_rows': len(o['positions']['rows']), 'after_rows': len(after['positions']['rows']),
                'sum_rub': after['sum'] / 100, 'store': after['store']['name'], 'state': after['state']['name'],
                'modes': [line['mode'] for line in plan['lines']], 'http': response.get('status'),
                'selected': [{'external_code': cards[ms_api.meta_id(p, 'assortment')]['externalCode'],
                    'quantity': p['quantity'], 'ms_id': ms_api.meta_id(p, 'assortment'),
                    'supplier': supplier(cards[ms_api.meta_id(p, 'assortment')])[1],
                    'brand': brand(cards[ms_api.meta_id(p, 'assortment')])[1]} for p in positions]}
            print('Decomposed:', o['name'], len(positions), 'rows', flush=True)
        except Exception as exc:
            result[cid] = {'name': snapshot['name'], 'action': 'held_for_review', 'changed': (folder / 'positions_intent.json').exists(), 'error': str(exc)[:300]}
            print('Review:', snapshot['name'], str(exc)[:200], flush=True)
        dump(OUT / 'progress.json', result)
    dump(OUT / 'result.json', {'orders': result, 'finished_at': datetime.now(timezone.utc).isoformat(),
        'counts': dict(Counter(r['action'] for r in result.values()))})
    print('Finished:', dict(Counter(r['action'] for r in result.values())), flush=True)


if __name__ == '__main__':
    prepare() if sys.argv[1] == 'prepare' else apply()

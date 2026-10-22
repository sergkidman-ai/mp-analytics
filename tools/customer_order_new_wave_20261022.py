# поток: inv
"""Fresh pending-order scan: keep original products only, no substitutions or bundle splitting."""
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from core import ms_api
from tools.customer_order_stock_plan_20261022 import tc
from tools.customer_order_batch17_20261022 import ROOT, dump, read

OUT = ROOT / 'new_orders_wave_1'


def main():
    assert not (OUT / 'scope.json').exists(), 'Fresh wave already frozen'
    OUT.mkdir(exist_ok=True); OUT.chmod(0o700)
    state = next(s for s in read(ROOT / 'metadata.json')['states'] if s['name'] == 'Ожидает подтверждения')
    orders, offset = [], 0
    while True:
        obj = ms_api.get('/entity/customerorder', {'limit': 20, 'offset': offset,
            'filter': 'state=' + state['meta']['href'], 'order': 'moment,asc',
            'expand': 'state,store,positions.assortment'})
        dump(OUT / f'pending_{offset:04}.json', obj)
        orders += obj['rows']; offset += len(obj['rows'])
        if offset >= obj['meta']['size']: break
    candidates, excluded, code_kind = [], {}, {}
    for order in orders:
        cid = order['id']
        try:
            assert order.get('applicable') and order.get('store'), 'Не проведён или не указан склад'
            assert order['positions']['rows'] and len(order['positions']['rows']) == order['positions']['meta']['size'], 'Неполные позиции'
            for row in order['positions']['rows']:
                card = row['assortment']; code = card.get('externalCode')
                assert card['meta']['type'] == 'product' and code and not card.get('archived'), 'Неподдерживаемая/архивная позиция'
                assert not row.get('pack'), 'Позиция в упаковках требует отдельной проверки'
                if code not in code_kind:
                    payload = tc(code)
                    if isinstance(payload, list): code_kind[code] = 'set'
                    elif isinstance(payload, dict) and payload.get('error') == 'not_mix': code_kind[code] = 'single'
                    else: code_kind[code] = 'unknown'
                assert code_kind[code] == 'single', 'Набор либо состав не подтверждён — замены не разрешены этой волной'
            candidates.append(order)
        except Exception as exc:
            excluded[cid] = {'name': order['name'], 'reason': str(exc)[:200]}
    stock, stores = {}, {}
    for order in candidates:
        sid = order['store']['id']; stores[sid] = order['store']
        for row in order['positions']['rows']:
            stock.setdefault(sid, {})[row['assortment']['id']] = row['assortment']['meta']['href']
    inventory = {}
    for sid, refs in stock.items():
        ids = sorted(refs)
        for start in range(0, len(ids), 15):
            chunk = ids[start:start + 15]
            obj = ms_api.get('/report/stock/all', {'limit': 100,
                'filter': ['store=' + stores[sid]['meta']['href'], 'stockMode=all']
                + ['product=' + refs[c] for c in chunk]})
            assert obj['meta']['size'] == len(obj['rows'])
            dump(OUT / f'stock_{sid}_{start:04}.json', obj)
            for cid in chunk: inventory[sid + '/' + cid] = {'stock': 0, 'reserve': 0}
            for row in obj['rows']:
                cid = row['meta']['href'].split('?')[0].rsplit('/', 1)[-1]
                assert cid in chunk
                inventory[sid + '/' + cid] = row
    pool = {k: Decimal(str(s['stock'])) - Decimal(str(s['reserve'])) for k, s in inventory.items()}
    used, selected = Counter(), []
    for order in candidates:
        sid = order['store']['id']; need, own = Counter(), Counter(); trial = dict(pool)
        for row in order['positions']['rows']:
            key = sid + '/' + row['assortment']['id']; q = Decimal(str(row['quantity']))
            need[key] += q; own[key] += min(q, Decimal(str(row.get('reserve', 0))))
        for key, q in own.items(): trial[key] += q
        enough = all(q > 0 and trial[k] >= q and Decimal(str(inventory[k]['stock'])) - used[k] >= q for k, q in need.items())
        if enough:
            for key, q in need.items(): trial[key] -= q; used[key] += q
            pool = trial; selected.append(order)
        else:
            excluded[order['id']] = {'name': order['name'], 'reason': 'Недостаточно исходного товара с учётом резервов и распределения'}
    dump(OUT / 'inventory.json', inventory)
    dump(OUT / 'scope_originals.json', {o['id']: o for o in selected})
    dump(OUT / 'scope.json', {'order_ids': [o['id'] for o in selected], 'captured_at': datetime.now(timezone.utc).isoformat(),
        'pending_count': len(orders), 'selected': len(selected), 'selection': 'Original products only; no bundles; shared physical stock and reserves checked'})
    dump(OUT / 'excluded.json', excluded)
    print('Fresh pending:', len(orders), 'eligible unchanged:', len(selected), 'excluded:', len(excluded), flush=True)


if __name__ == '__main__':
    main()

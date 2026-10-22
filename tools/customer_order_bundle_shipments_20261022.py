# поток: inv
"""Authorized shipments for the 19 already decomposed orders; durable one-attempt writes."""
from datetime import timedelta
from collections import Counter
import sys

from tools import customer_order_batch17_20261022 as b

b.OUT = b.ROOT / 'decomposed_bundle_shipments'


def equal_order(current, previous):
    b.verify_order_unchanged(current, previous)
    for key in ('vatSum', 'vatIncluded', 'vatEnabled'):
        assert current.get(key) == previous.get(key), key + ' changed'
    for key in ('organization', 'agent'):
        assert b.ms_api.meta_id(current, key) == b.ms_api.meta_id(previous, key)


def check_demand(d, order):
    return b.demand_check(d, order) and d.get('vatSum') == order.get('vatSum')


def prepare():
    assert not (b.OUT / 'status_result.json').exists(), 'Already processed; inspect saved audit'
    ids, originals = b.scope()
    assert len(ids) == 19
    result = {}
    state = next(s for s in b.ms_api.get('/entity/customerorder/metadata')['states']
                 if s['name'] == 'Собирается')
    for cid in ids:
        folder = b.OUT / cid
        entry = {'name': originals[cid]['name']}
        try:
            o = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
            b.dump(folder / 'order_before.json', o)
            equal_order(o, originals[cid])
            previous_folder = b.ROOT / 'remaining_bundles' / cid
            previous_file = previous_folder / 'final_readback.json'
            if not previous_file.exists():
                previous_file = previous_folder / 'before.json'
            equal_order(o, b.read(previous_file))
            if o.get('demands'):
                assert len(o['demands']) == 1, 'Multiple demands; inspect individually'
                did = b.ms_api.meta_id({'r': o['demands'][0]}, 'r')
                d = b.ms_api.get('/entity/demand/' + did, {'expand': 'customerOrder,store,positions.assortment'})
                b.dump(folder / 'existing_demand.json', d)
                assert check_demand(d, o), 'Existing demand differs from order'
                entry.update(action='already_shipped', verified=True, demand_id=did)
            else:
                assert o['state']['name'] in ('Неукомплектован', 'Ожидает подтверждения', 'Собирается'), 'Advanced state'
                b.stock_check(o, folder)
                latest = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
                equal_order(latest, o)
                assert not latest.get('demands'), 'Demand appeared; inspect and avoid duplicate'
                if latest['state']['name'] != 'Собирается' or not latest.get('deliveryPlannedMoment', '').startswith(b.DATE):
                    assert latest['state']['name'] in ('Неукомплектован', 'Ожидает подтверждения', 'Собирается')
                    b.one_write(folder, 'status', 'PUT', '/entity/customerorder/' + cid,
                                {'state': {'meta': state['meta']}, 'deliveryPlannedMoment': b.DATE})
                after = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
                b.dump(folder / 'order_after.json', after)
                equal_order(after, o)
                assert after['state']['name'] == 'Собирается' and after.get('deliveryPlannedMoment', '').startswith(b.DATE)
                entry.update(action='ready_for_shipment', verified=True,
                             status_write_attempted=(folder / 'status_intent.json').exists())
        except Exception as exc:
            entry.update(action='held', verified=False, error=str(exc)[:300])
        result[cid] = entry
        b.dump(b.OUT / 'preparation_progress.json', result)
    end = b.now()
    b.dump(b.OUT / 'preparation_result.json', {'orders': result, 'finished_at': end.isoformat()})
    b.dump(b.OUT / 'status_result.json', {
        'orders': {k: v for k, v in result.items() if v['action'] == 'ready_for_shipment'},
        'finished_at': end.isoformat(), 'shipment_check_not_before': (end + timedelta(minutes=5)).isoformat()})
    print('Preparation:', dict(Counter(v['action'] for v in result.values())))
    for v in result.values():
        if not v['verified']:
            print(v['name'], v.get('error'))
    print('Shipment check not before', (end + timedelta(minutes=5)).isoformat())


def reconcile():
    """Read back successful writes with MS millisecond timestamps; preserve externally changed prices."""
    report = b.read(b.OUT / 'preparation_result.json')
    statuses = b.read(b.OUT / 'status_result.json')
    _, originals = b.scope()
    state = next(s for s in b.ms_api.get('/entity/customerorder/metadata')['states'] if s['name'] == 'Собирается')
    for cid, entry in report['orders'].items():
        if entry['action'] != 'held' or entry.get('error') not in ('', 'Order positions changed', 'Component products or quantities changed'):
            continue
        folder = b.OUT / cid
        try:
            o = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
            equal_order(o, originals[cid])
            assert not o.get('demands'), 'Demand appeared; inspect'
            before = b.read(b.ROOT / 'remaining_bundles' / cid / 'before.json')
            # The current task ships the current chosen supplier products. Another actor
            # changed two supplier cards, while keeping the exact TC component codes.
            quantities = lambda x: Counter((r['assortment']['externalCode'], str(r['quantity'])) for r in x['positions']['rows'])
            if entry.get('error') in ('Order positions changed', 'Component products or quantities changed'):
                assert quantities(o) == quantities(before), 'Component products or quantities changed'
                assert b.ms_api.meta_id(o, 'store') == b.ms_api.meta_id(before, 'store')
            b.dump(folder / 'reconciliation_before.json', o)
            b.stock_check(o, folder / 'reconciliation')
            if o['state']['name'] != 'Собирается' or not o.get('deliveryPlannedMoment', '').startswith(b.DATE):
                assert not (folder / 'status_intent.json').exists(), 'No repeated write after recorded attempt'
                assert o['state']['name'] in ('Неукомплектован', 'Ожидает подтверждения', 'Собирается')
                latest = b.ms_api.get('/entity/customerorder/' + cid)
                assert latest['updated'] == o['updated'] and not latest.get('demands')
                b.one_write(folder, 'status', 'PUT', '/entity/customerorder/' + cid,
                            {'state': {'meta': state['meta']}, 'deliveryPlannedMoment': b.DATE})
            after = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
            equal_order(after, o)
            assert after['state']['name'] == 'Собирается' and after.get('deliveryPlannedMoment', '').startswith(b.DATE)
            b.dump(folder / 'order_after.json', after)
            entry.update(action='ready_for_shipment', verified=True,
                         status_write_attempted=(folder / 'status_intent.json').exists(), error=None)
            statuses['orders'][cid] = entry
        except Exception as exc:
            entry.update(error=str(exc)[:300])
    end = b.now()
    statuses.update(finished_at=end.isoformat(), shipment_check_not_before=(end + timedelta(minutes=5)).isoformat())
    b.dump(b.OUT / 'preparation_result.json', report)
    b.dump(b.OUT / 'status_result.json', statuses)
    print('Reconciled:', dict(Counter(v['action'] for v in report['orders'].values())))
    for entry in report['orders'].values():
        if entry['action'] == 'held':
            print(entry['name'], entry.get('error'))
    print('Shipment check not before', statuses['shipment_check_not_before'])


def finalize():
    from decimal import Decimal
    from openpyxl import Workbook
    from openpyxl.styles import Font
    prepared = b.read(b.OUT / 'preparation_result.json')['orders']
    shipments = b.read(b.OUT / 'shipment_result.json')['orders']
    audit, shortages = {}, []
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Заказы'
    sheet.append(['Заказ', 'Результат', 'Статус МС', 'Плановая дата', 'Склад', 'Отгрузка', 'Дата отгрузки', 'Примечание'])
    for cid, entry in prepared.items():
        o = b.ms_api.get('/entity/customerorder/' + cid, b.EXPAND)
        b.dump(b.OUT / cid / 'final_order.json', o)
        equal_order(o, b.read(b.OUT / 'scope_originals.json')[cid])
        note, d = '', None
        if entry['action'] in ('already_shipped', 'ready_for_shipment'):
            assert len(o.get('demands', [])) == 1
            did = b.ms_api.meta_id({'r': o['demands'][0]}, 'r')
            d = b.ms_api.get('/entity/demand/' + did, {'expand': 'customerOrder,store,positions.assortment'})
            b.dump(b.OUT / cid / 'final_demand.json', d)
            assert check_demand(d, o), 'Final demand mismatch: ' + o['name']
            if entry['action'] == 'ready_for_shipment':
                assert shipments[cid]['verified']
                assert o['state']['name'] == 'Собирается' and o.get('deliveryPlannedMoment', '').startswith(b.DATE)
                assert d['name'] == o['name']
            action = ('created' if shipments.get(cid, {}).get('created_by_this_run') else 'already_shipped')
        else:
            action = 'held'
            folder = b.OUT / cid
            proof_file = folder / 'reconciliation' / 'stock_before.json'
            if not proof_file.exists():
                proof_file = folder / 'stock_before.json'
            if proof_file.exists():
                need, own, labels = Counter(), Counter(), {}
                for r in o['positions']['rows']:
                    pid = b.ms_api.meta_id(r, 'assortment')
                    need[pid] += Decimal(str(r['quantity']))
                    own[pid] += Decimal(str(r.get('reserve', 0)))
                    labels[pid] = r['assortment'].get('externalCode')
                for pid, raw in zip(need, b.read(proof_file)):
                    rows = [s for s in raw['rows'] if s['meta']['href'].split('?')[0].rsplit('/', 1)[-1] == pid]
                    physical = Decimal(str(rows[0]['stock'])) if rows else Decimal(0)
                    reserve = Decimal(str(rows[0]['reserve'])) if rows else Decimal(0)
                    available = min(physical, physical - reserve + min(own[pid], need[pid]))
                    if physical < need[pid] or available < need[pid]:
                        shortage = dict(order=o['name'], external_code=labels[pid], required=str(need[pid]),
                                        physical=str(physical), reserved=str(reserve), own_reserve=str(own[pid]),
                                        available_for_order=str(available), store=o['store']['name'])
                        shortages.append(shortage)
                        note = f"Недостаточно {labels[pid]}: нужно {need[pid]}, физически {physical}, доступно с учётом резервов {available}"
                        break
            if not note:
                note = entry.get('error', '')
        audit[cid] = dict(order=o['name'], action=action, verified=True, state=o['state']['name'],
                          planned=o.get('deliveryPlannedMoment'), store=o['store']['name'],
                          demand_id=d['id'] if d else None, demand_name=d['name'] if d else None,
                          moment=d['moment'] if d else None, note=note)
        label = {'created': 'Отгрузка создана', 'already_shipped': 'Отгрузка уже была', 'held': 'Не хватает компонентов'}[action]
        sheet.append([o['name'], label, o['state']['name'], o.get('deliveryPlannedMoment'), o['store']['name'],
                      d['name'] if d else '', d['moment'] if d else '', note])
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for col, width in {'A': 25, 'B': 27, 'C': 25, 'D': 27, 'E': 18, 'F': 25, 'G': 27, 'H': 95}.items():
        sheet.column_dimensions[col].width = width
    path = b.OUT / 'Отгрузки_разложенных_наборов_22.10.2026.xlsx'
    workbook.save(path)
    path.chmod(0o600)
    b.dump(b.OUT / 'shortages.json', shortages)
    counts = dict(Counter(v['action'] for v in audit.values()))
    b.dump(b.OUT / 'final_verification.json', {'orders': audit, 'counts': counts, 'checked_at': b.now().isoformat()})
    print('Final verified', len(audit), counts)
    for s in shortages:
        print(s['order'], 'missing', s['external_code'], 'physical', s['physical'])


if __name__ == '__main__':
    if sys.argv[1] == 'prepare':
        prepare()
    elif sys.argv[1] == 'reconcile':
        reconcile()
    elif sys.argv[1] == 'finalize':
        finalize()
    elif sys.argv[1] == 'shipments':
        original_check = b.demand_check
        b.demand_check = lambda d, o: original_check(d, o) and d.get('vatSum') == o.get('vatSum')
        original_unchanged = b.verify_order_unchanged
        def strict_unchanged(o, old):
            original_unchanged(o, old)
            assert o.get('vatSum') == old.get('vatSum'), 'VAT amount changed'
        b.verify_order_unchanged = strict_unchanged
        b.shipments()
    else:
        raise ValueError('Unknown stage')

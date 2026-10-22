# поток: inv
"""Authorized 17 unchanged orders only; one write attempt, durable audit, no blind retry."""
import json
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import requests

from core import ms_api

ROOT = Path('/opt/mp-analytics/docs/reports/customer_order_stock_plan_20261022')
OUT = ROOT / 'batch_17'
DATE = '2026-10-22 00:00:00'
EXPAND = {'expand': 'state,store,positions.assortment'}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    path.chmod(0o600)


def read(path):
    return json.loads(path.read_text())


def now():
    return datetime.now(timezone.utc)


def signature(rows):
    return Counter((ms_api.meta_id(r, 'assortment'), Decimal(str(r['quantity'])),
        Decimal(str(r['price'])), Decimal(str(r.get('discount', 0))), r.get('vat'), r.get('vatEnabled')) for r in rows)


def one_write(folder, label, method, path, body):
    intent = folder / (label + '_intent.json')
    assert not intent.exists(), 'Write attempt already recorded; do not repeat'
    dump(intent, {'at': now().isoformat(), 'method': method, 'path': path, 'body': body})
    try:
        r = requests.request(method, ms_api.BASE + path,
            headers={'Authorization': 'Bearer ' + ms_api.TOKEN, 'Content-Type': 'application/json', 'Accept-Encoding': 'gzip'},
            json=body, timeout=(5, 60), allow_redirects=False)
        result = {'status': r.status_code, 'body': r.json() if r.content else None}
    except requests.RequestException as exc:
        result = {'uncertain': True, 'exception': type(exc).__name__}
    dump(folder / (label + '_response.json'), result)
    return result


def stock_check(order, folder):
    need, reserve, refs = Counter(), Counter(), {}
    for r in order['positions']['rows']:
        cid = ms_api.meta_id(r, 'assortment')
        need[cid] += Decimal(str(r['quantity']))
        reserve[cid] += Decimal(str(r.get('reserve', 0)))
        refs[cid] = r['assortment']['meta']['href']
    proof = []
    for cid, qty in need.items():
        obj = ms_api.get('/report/stock/all', {'limit': 20, 'filter': [
            'store=' + order['store']['meta']['href'], 'product=' + refs[cid], 'stockMode=all']})
        proof.append(obj)
        dump(folder / 'stock_before.json', proof)
        rows = [r for r in obj['rows'] if r['meta']['href'].split('?')[0].rsplit('/', 1)[-1] == cid]
        assert len(rows) == 1, 'No stock row'
        s = rows[0]; physical = Decimal(str(s['stock']))
        assert physical >= qty and physical - Decimal(str(s['reserve'])) + min(reserve[cid], qty) >= qty, 'Insufficient current stock'
    dump(folder / 'stock_before.json', proof)


def scope():
    if (OUT / 'scope.json').exists():
        ids = read(OUT / 'scope.json')['order_ids']
        originals = read(OUT / 'scope_originals.json')
        assert ids and len(ids) == len(set(ids)) and all(cid in originals for cid in ids)
        return ids, originals
    reports = read(ROOT / 'order_report.json')
    ids = [r['ID заказа'] for r in reports if r['Готов'] and r['Действие'] == 'Без замены'
           and r['ID заказа'] != '65251555-cd60-11f1-0a80-0bdf008625c3']
    assert len(ids) == 17 and len(set(ids)) == 17
    originals = {o['id']: o for f in ROOT.glob('orders_*.json') for o in read(f)['rows']}
    return ids, originals


def verify_order_unchanged(o, original):
    assert ms_api.meta_id(o, 'store') == ms_api.meta_id(original, 'store'), 'Warehouse changed'
    assert o['sum'] == original['sum'], 'Order amount changed'
    assert len(o['positions']['rows']) == o['positions']['meta']['size']
    assert signature(o['positions']['rows']) == signature(original['positions']['rows']), 'Order positions changed'


def update():
    OUT.mkdir(exist_ok=True); OUT.chmod(0o700)
    assert not (OUT / 'status_result.json').exists(), 'Stage finished already'
    ids, originals = scope(); result = {}
    metadata = ms_api.get('/entity/customerorder/metadata')
    state = next(s for s in metadata['states'] if s['name'] == 'Собирается')
    payload = {'state': {'meta': state['meta']}, 'deliveryPlannedMoment': DATE}
    for cid in ids:
        folder = OUT / cid
        try:
            o = ms_api.get('/entity/customerorder/' + cid, EXPAND)
            dump(folder / 'order_before.json', o)
            verify_order_unchanged(o, originals[cid])
            name = o['state']['name']
            assert name in ('Ожидает подтверждения', 'Собирается'), 'Order advanced to another state'
            if name != 'Собирается' or not o.get('deliveryPlannedMoment', '').startswith(DATE):
                assert not o.get('demands'), 'Shipment already exists; inspect before changing order'
                stock_check(o, folder)
                latest = ms_api.get('/entity/customerorder/' + cid)
                assert latest['updated'] == o['updated'], 'Order changed during preflight'
                one_write(folder, 'status', 'PUT', '/entity/customerorder/' + cid, payload)
            after = ms_api.get('/entity/customerorder/' + cid, EXPAND)
            dump(folder / 'order_after.json', after)
            verify_order_unchanged(after, o)
            assert after['state']['name'] == 'Собирается' and after.get('deliveryPlannedMoment', '').startswith(DATE)
            result[cid] = {'name': after['name'], 'verified': True, 'status_write_attempted': (folder / 'status_intent.json').exists()}
        except Exception as exc:
            result[cid] = {'verified': False, 'error': str(exc)[:300]}
        dump(OUT / 'status_progress.json', result)
        print('Status', cid, result[cid].get('verified'), flush=True)
    finished = now()
    dump(OUT / 'status_result.json', {'orders': result, 'finished_at': finished.isoformat(),
        'shipment_check_not_before': (finished + timedelta(minutes=5)).isoformat()})
    print('Status stage finished:', sum(v['verified'] for v in result.values()), '/', len(ids), ';',
          'check not before', (finished + timedelta(minutes=5)).isoformat(), flush=True)


def demand_check(d, order):
    rows = d['positions']['rows']
    return (ms_api.meta_id(d, 'customerOrder') == order['id']
        and ms_api.meta_id(d, 'store') == ms_api.meta_id(order, 'store')
        and ms_api.meta_id(d, 'organization') == ms_api.meta_id(order, 'organization')
        and ms_api.meta_id(d, 'agent') == ms_api.meta_id(order, 'agent')
        and d['sum'] == order['sum'] and signature(rows) == signature(order['positions']['rows'])
        and bool(d.get('applicable')))


def shipments():
    statuses = read(OUT / 'status_result.json')
    assert now() >= datetime.fromisoformat(statuses['shipment_check_not_before']), 'Five minutes have not elapsed'
    assert not (OUT / 'shipment_result.json').exists(), 'Shipment stage already finished'
    result = {}
    for cid, status in statuses['orders'].items():
        folder = OUT / cid / 'shipment'
        try:
            assert status['verified'], 'Status stage was not verified'
            o = ms_api.get('/entity/customerorder/' + cid, EXPAND)
            dump(folder / 'order_before.json', o)
            verify_order_unchanged(o, read(OUT / cid / 'order_after.json'))
            refs = o.get('demands', [])
            created = False
            if not refs:
                assert o['state']['name'] == 'Собирается', 'Order advanced; do not create shipment'
                same = ms_api.get('/entity/demand', {'limit': 20, 'filter': [
                    'name=' + o['name'], 'organization=' + o['organization']['meta']['href']]})
                dump(folder / 'same_number_check.json', same)
                matching = [d for d in same['rows'] if ms_api.meta_id(d, 'customerOrder') == cid]
                if matching:
                    refs = [{'meta': d['meta']} for d in matching]
                else:
                    assert not same['rows'], 'Another demand already has this number'
                    stock_check(o, folder)
                    # PUT /new returns a template; it does not create an entity.
                    template = ms_api.put('/entity/demand/new', {'customerOrder': {'meta': o['meta']}})
                    dump(folder / 'template.json', template)
                    rows = template['positions']['rows'] if isinstance(template['positions'], dict) else template['positions']
                    assert signature(rows) == signature(o['positions']['rows'])
                    assert ms_api.meta_id(template, 'customerOrder') == cid
                    latest = ms_api.get('/entity/customerorder/' + cid)
                    if latest.get('demands'):
                        refs = latest['demands']
                    else:
                        assert latest['updated'] == o['updated'], 'Order changed during shipment preflight'
                        payload = {k: v for k, v in template.items() if k not in {'sum', 'payedSum', 'printed', 'published', 'files'}}
                        payload.update(positions=rows, name=o['name'], moment=datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d %H:%M:%S'))
                        response = one_write(folder, 'create', 'POST', '/entity/demand', payload)
                        if response.get('status') in (200, 201):
                            refs = [{'meta': response['body']['meta']}]; created = True
                        else:
                            last = ms_api.get('/entity/customerorder/' + cid)
                            refs = last.get('demands', [])
                            assert refs, 'Creation failed or uncertain; no repeated POST'
            assert len(refs) == 1, 'Multiple related demands require manual review'
            did = ms_api.meta_id({'r': refs[0]}, 'r')
            d = ms_api.get('/entity/demand/' + did, {'expand': 'customerOrder,store,positions.assortment'})
            dump(folder / 'demand_after.json', d)
            assert demand_check(d, o), 'Related demand differs from order'
            after = ms_api.get('/entity/customerorder/' + cid)
            assert did in [ms_api.meta_id({'r': r}, 'r') for r in after.get('demands', [])]
            dump(folder / 'order_after.json', after)
            result[cid] = {'verified': True, 'order_name': o['name'], 'demand_id': did,
                'demand_name': d['name'], 'moment': d['moment'], 'created_by_this_run': created,
                'creation_attempted': (folder / 'create_intent.json').exists(), 'applicable': d['applicable']}
        except Exception as exc:
            result[cid] = {'verified': False, 'error': str(exc)[:300]}
        dump(OUT / 'shipment_progress.json', result)
        print('Shipment', cid, result[cid].get('verified'), flush=True)
    dump(OUT / 'shipment_result.json', {'checked_at': now().isoformat(), 'orders': result})
    print('Shipments verified:', sum(v['verified'] for v in result.values()), '/', len(statuses['orders']), ';',
          'created:', sum(v.get('created_by_this_run', False) for v in result.values()), flush=True)


if __name__ == '__main__':
    if len(sys.argv) > 2:
        assert sys.argv[2] == 'new_orders_wave_1'
        OUT = ROOT / sys.argv[2]
    update() if sys.argv[1] == 'update' else shipments()

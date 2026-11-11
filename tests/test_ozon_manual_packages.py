# поток: inv
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from contextlib import nullcontext

import requests

from ops import ozon_manual_packages as p
from ops import fbs_market_fulfillment as f
from tests.test_fbs_market_fulfillment import order as adapter_order, response
from core import ms_api
from shipment_guard.config import ORGS, AGENTS


def posting(number='12345-0001-1', qty=10, price='100', sku=101, offer='1000'):
    return {'posting_number': number, 'order_id': 12345, 'status': 'awaiting_packaging',
            'parent_posting_number': '', 'related_postings': {'related_posting_numbers': []},
            'products': [{'sku': sku, 'offer_id': offer, 'quantity': qty, 'price': price}]}


def msorder(qty=10, price=10000, code='1000', oid='11111111-1111-4111-8111-111111111111'):
    row = {'assortment': dict(ms_api.ref('product', '22222222-2222-4222-8222-222222222222'),
                             externalCode=code), 'quantity': qty, 'price': price, 'discount': 0, 'vat': 5}
    return {'id': oid, 'meta': ms_api.ref('customerorder', oid)['meta'], 'updated': 'v1',
            'name': '12345-0001-1', 'sum': qty * price,
            'organization': ms_api.ref('organization', list(ORGS)[0]),
            'agent': ms_api.ref('counterparty', next(k for k, v in AGENTS.items() if v == 'ozon')),
            'store': ms_api.ref('store', 'store1'), 'state': dict(p.state(p.HOLD), name='Не собран в ЛК Озон'),
            'vatEnabled': True, 'vatIncluded': True,
            'positions': {'meta': {'size': 1}, 'rows': [row]}, 'attributes': []}


def split(original, counts=(4, 6), retain_original=True):
    names = [original['posting_number'] if i == 0 and retain_original else f'12345-0001-{i + 2}' for i in range(len(counts))]
    result = []
    for name, count in zip(names, counts):
        x = deepcopy(original)
        x.update(posting_number=name, status='awaiting_deliver',
                 parent_posting_number='' if name == original['posting_number'] else original['posting_number'])
        x['products'][0]['quantity'] = count
        x['related_postings']['related_posting_numbers'] = [n for n in names if n != name]
        result.append(x)
    return result


class Backend:
    def __init__(self, order):
        self.docs = {('customerorder', order['id']): deepcopy(order)}
        did = '33333333-3333-4333-8333-333333333333'
        dem = deepcopy(order)
        dem.update(id=did, meta=ms_api.ref('demand', did)['meta'], applicable=True,
                   customerOrder=ms_api.ref('customerorder', order['id']))
        dem.pop('state', None)
        self.docs[('demand', did)] = dem
        self.docs[('customerorder', order['id'])]['demands'] = [ms_api.ref('demand', did)]
        self.writes = []
        self.fail_at = None
        self.lost_response_at = None
        self.stock = 1000
        self.update_linked_order = False

    def get(self, path, params=None):
        if path == '/report/stock/all':
            product = next(x[len('product='):] for x in params['filter'] if x.startswith('product='))
            return {'rows': [{'meta': {'href':product}, 'stock': self.stock, 'reserve': 0}]}
        bits = path.split('/')
        kind = bits[2]
        if len(bits) == 4:
            key = kind, bits[3]
            if key not in self.docs:
                raise ms_api.MsError(404, {})
            return deepcopy(self.docs[key])
        filters = params['filter']
        name = next(x[5:] for x in filters if x.startswith('name='))
        org = next(x[len('organization='):] for x in filters if x.startswith('organization='))
        rows = [deepcopy(x) for (k, _), x in self.docs.items() if k == kind and x['name'] == name and x['organization']['meta']['href'] == org]
        return {'rows': rows, 'meta': {'size': len(rows)}}

    def request(self, method, url, json, **kwargs):
        if self.fail_at is not None and len(self.writes) == self.fail_at:
            raise requests.ReadTimeout()
        self.writes.append((method, url, deepcopy(json)))
        parts = url.split('/entity/')[1].split('/')
        kind = parts[0]
        uid = parts[1] if len(parts) > 1 else json['id']
        doc = deepcopy(self.docs.get((kind, uid), {}))
        doc.update(deepcopy(json), id=uid, meta=ms_api.ref(kind, uid)['meta'], updated='v' + str(len(self.writes) + 1))
        if isinstance(doc.get('positions'), list):
            rows = doc['positions']
            doc['positions'] = {'rows': rows, 'meta': {'size': len(rows)}}
            doc['sum'] = p.amount(rows)
        if 'state' in json:
            doc['state']['name'] = 'Не собран в ЛК Озон' if ms_api.meta_id(doc, 'state') == p.HOLD else 'Собирается'
        if kind == 'customerorder':
            doc.setdefault('demands', [])
        else:
            oid = ms_api.meta_id(doc, 'customerOrder')
            co = self.docs.get(('customerorder', oid))
            if co is not None:
                ref = ms_api.ref('demand', uid)
                if ref not in co['demands']:
                    co['demands'].append(ref)
                if self.update_linked_order:
                    co['updated'] = 'auto-link-' + str(len(self.writes))
                    co['shippedSum'] = doc['sum'] if doc['applicable'] else 0
                    for row in co['positions']['rows']:
                        row['shipped'] = row['quantity'] if doc['applicable'] else 0
        self.docs[(kind, uid)] = doc
        if self.lost_response_at == len(self.writes):
            raise requests.ReadTimeout()
        return Mock(status_code=200)


class PackagesTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(p, 'RUNTIME', self.root)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(p, 'assert_automatic_composition')
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(p.ms_api_control, 'request', side_effect=lambda *a, sender, **kw: sender(*a, **{k:v for k,v in kw.items() if k not in ('client','kind')}))
        patcher.start(); self.addCleanup(patcher.stop)

    def test_exact_boundaries_and_mixed_sku_units(self):
        self.assertFalse(p.needs_manual(posting(qty=8, price='3750')))
        self.assertTrue(p.needs_manual(posting(qty=8, price='3750.01')))
        self.assertTrue(p.needs_manual(posting(qty=9, price='1')))
        x = posting(qty=5)
        x['products'].append({'sku': 102, 'offer_id': '1001', 'quantity': 5, 'price': '1'})
        self.assertTrue(p.needs_manual(x))

    def test_invalid_price_cannot_bypass_gate(self):
        for value in (None, 'NaN', '-1', 'Infinity'):
            with self.subTest(value=value), self.assertRaises(p.Deferred):
                p.needs_manual(posting(qty=1, price=value))

    def test_partition_preserves_manual_supplier_and_bundle_quantity(self):
        x = posting(qty=10)
        family = split(x)
        order = msorder(qty=30, price=10000 / 3)
        # Use exact integral per-unit physical prices for a bundle of three.
        x['products'][0]['price'] = '99'
        for child in family:
            child['products'][0]['price'] = '99'
        order['positions']['rows'][0]['price'] = 3300
        order['sum'] = 99000
        result = p.partition(order, x, family)
        self.assertEqual([rows[0]['quantity'] for rows in result.values()], [12, 18])
        self.assertEqual(p.signature(sum(result.values(), [])), p.signature(order['positions']['rows']))

    def test_same_base_ambiguous_offers_are_not_guessed(self):
        x = posting(qty=5, offer='1000AAA')
        x['products'].append(dict(x['products'][0], sku=102, offer_id='1000BBB'))
        with self.assertRaisesRegex(p.Deferred, 'ambiguous_ms'):
            p.partition(msorder(), x, [x])

    def test_child_money_mismatch_blocks_all_writes(self):
        x = posting(); family = split(x)
        family[0]['products'][0]['price'] = '101'
        with self.assertRaisesRegex(p.Deferred, 'posting_amount'):
            p.partition(msorder(), x, family)

    def test_gate_never_sends_ship_gtd_or_exemplars_for_large_order(self):
        original = posting()
        o = adapter_order()
        original['posting_number'] = o['name']
        register = Mock(return_value={'result':'waiting_operator'})
        request = Mock(return_value=response({'result': original}))
        with patch.object(f, 'account_lock', return_value=nullcontext(Mock())), patch.object(p.Reconciler, 'register', register):
            session = f.FulfillmentSession(request, sleep=lambda _: None)
            result = session.fulfill(o, self.root / 'adapter')
        self.assertEqual(result['reason'], 'ozon_manual_split_waiting_operator')
        self.assertEqual(request.call_count, 1)
        self.assertTrue(request.call_args.args[1].endswith('/v3/posting/fbs/get'))
        register.assert_called_once()

    def test_register_sets_two_places_and_keeps_positions(self):
        order = msorder(); order['state'] = dict(p.state(p.ASSEMBLING), name='Собирается')
        backend = Backend(order)
        rec = p.Reconciler(get=backend.get, request=backend.request)
        before = p.signature(order['positions']['rows'])
        rec.register(order, posting(), 'oz_acc1', Mock())
        saved = backend.docs[('customerorder', order['id'])]
        self.assertEqual(ms_api.meta_id(saved, 'state'), p.HOLD)
        self.assertEqual(saved['attributes'][-1]['value'], '2')
        self.assertEqual(p.signature(saved['positions']['rows']), before)
        rec.register(saved, posting(), 'oz_acc1', Mock())
        self.assertEqual(len(backend.writes), 1)

    def make_plan(self, family=None):
        original = posting()
        order = msorder()
        backend = Backend(order)
        rec = p.Reconciler(get=backend.get, request=backend.request)
        folder = self.root / 'case'; folder.mkdir()
        registration = {'account': 'oz_acc1', 'number': original['posting_number'],
                        'order_id': order['id'], 'posting': original}
        return rec, backend, folder, rec.plan(registration, family or split(original), folder)

    def test_complete_partition_and_repeat_do_not_duplicate_docs(self):
        rec, backend, folder, plan = self.make_plan()
        value = rec.apply_plan(plan, folder, Mock())
        count = len(backend.writes)
        self.assertEqual(value['places'], 2)
        self.assertEqual(len(backend.docs), 4)
        rec.apply_plan(plan, folder, Mock())
        self.assertEqual(len(backend.writes), count)
        orders = [d for (kind, _), d in backend.docs.items() if kind == 'customerorder']
        demands = [d for (kind, _), d in backend.docs.items() if kind == 'demand']
        self.assertEqual(sum(d['sum'] for d in orders), 100000)
        self.assertEqual(sum(d['sum'] for d in demands if d['applicable']), 100000)
        self.assertEqual({d['name'] for d in orders}, {'12345-0001-1','12345-0001-3'})
        self.assertTrue(all(ms_api.meta_id(d, 'state') == p.ASSEMBLING for d in orders))

    def test_three_actual_places_produce_three_orders_and_demands(self):
        rec, backend, folder, plan = self.make_plan(split(posting(), (2, 3, 5)))
        rec.apply_plan(plan, folder, Mock())
        self.assertEqual(len(backend.docs), 6)
        self.assertEqual(len([d for (k,_),d in backend.docs.items() if k == 'demand' and d['applicable']]), 3)

    def test_automatic_link_counters_are_reconciled_without_ignoring_manual_fields(self):
        rec, backend, folder, plan = self.make_plan()
        backend.update_linked_order = True
        rec.apply_plan(plan, folder, Mock())
        rec.apply_plan(plan, folder, Mock())
        self.assertEqual(len(backend.writes), 7)

    def test_manual_lock_is_inherited_by_all_children(self):
        with patch.object(p, 'assert_automatic_composition', side_effect=p.ManualCompositionProtected('manual')):
            rec, backend, folder, plan = self.make_plan()
        with patch.object(p, 'remember') as remember:
            rec.apply_plan(plan, folder, Mock())
        self.assertEqual({call.args[0]['order_id'] for call in remember.call_args_list},
                         {c['order_id'] for c in plan['children'] if c['order_id'] != plan['original_order']['id']})

    def test_history_failure_does_not_authorize_partition(self):
        with patch.object(p, 'assert_automatic_composition', side_effect=ValueError('audit unavailable')):
            with self.assertRaisesRegex(ValueError, 'audit unavailable'):
                self.make_plan()

    def test_resume_after_crash_after_each_successful_write(self):
        # Simulate process death between an acknowledged write and its local
        # verification checkpoint, at every mutation in a two-place split.
        for crash_at in range(1, 8):
            with self.subTest(crash_at=crash_at), TemporaryDirectory(dir=self.root) as temp:
                original = posting(); order = msorder(); backend = Backend(order)
                rec = p.Reconciler(get=backend.get, request=backend.request)
                folder = Path(temp)
                plan = rec.plan({'account':'oz_acc1','number':original['posting_number'],
                                'order_id':order['id'],'posting':original}, split(original), folder)
                send = backend.request
                def crash(*args, **kw):
                    value = send(*args, **kw)
                    if len(backend.writes) == crash_at:
                        raise KeyboardInterrupt()
                    return value
                rec.request = crash
                with self.assertRaises(KeyboardInterrupt):
                    rec.apply_plan(plan, folder, Mock())
                rec.request = send
                rec.apply_plan(plan, folder, Mock())
                self.assertEqual(len(backend.writes), 7)
                self.assertEqual(len(backend.docs), 4)

    def test_reuses_original_ids_when_ozon_replaces_original_number(self):
        rec, backend, folder, plan = self.make_plan(split(posting(), retain_original=False))
        rec.apply_plan(plan, folder, Mock())
        self.assertEqual(backend.docs[('customerorder', plan['original_order']['id'])]['name'], '12345-0001-2')
        self.assertEqual(len(backend.docs), 4)

    def test_unknown_write_without_fact_is_not_repeated(self):
        rec, backend, folder, plan = self.make_plan()
        backend.fail_at = 2
        with self.assertRaisesRegex(p.Deferred, 'write_not_verified'):
            rec.apply_plan(plan, folder, Mock())
        count = len(backend.writes)
        backend.fail_at = None
        with self.assertRaisesRegex(p.Deferred, 'uncertain_write_no_repeat'):
            rec.apply_plan(plan, folder, Mock())
        self.assertEqual(len(backend.writes), count)

    def test_lost_success_response_is_resolved_by_read(self):
        rec, backend, folder, plan = self.make_plan()
        backend.lost_response_at = 4
        rec.apply_plan(plan, folder, Mock())
        self.assertEqual(len(backend.docs), 4)

    def test_foreign_reservations_or_missing_stock_cannot_create_shipment(self):
        rec, backend, folder, plan = self.make_plan()
        backend.stock = 0
        with self.assertRaisesRegex(p.Deferred, 'fresh_stock_insufficient'):
            rec.apply_plan(plan, folder, Mock())
        self.assertEqual(len(backend.writes), 2)  # unpost + shrink order, no shipment

    def test_concurrent_manual_change_blocks_before_any_write(self):
        rec, backend, folder, plan = self.make_plan()
        oid = plan['original_order']['id']
        backend.docs[('customerorder', oid)]['updated'] = 'manual-new'
        backend.docs[('customerorder', oid)]['positions']['rows'][0]['price'] = 9000
        with self.assertRaisesRegex(p.Deferred, 'document_changed'):
            rec.apply_plan(plan, folder, Mock())
        self.assertEqual(backend.writes, [])

    def test_already_imported_child_blocks_before_unposting_original(self):
        original = posting(); order = msorder(); backend = Backend(order)
        child = deepcopy(order); child.update(id='foreign', name='12345-0001-3')
        backend.docs[('customerorder', 'foreign')] = child
        rec = p.Reconciler(get=backend.get, request=backend.request)
        with self.assertRaisesRegex(p.Deferred, 'already_exists'):
            rec.plan({'account':'oz_acc1','number':original['posting_number'],'order_id':order['id'],'posting':original}, split(original), self.root)
        self.assertEqual(backend.writes, [])

    def test_family_requires_complete_ready_quantities_and_explicit_order_id(self):
        original = posting(); children = split(original)
        reg = {'account':'oz_acc1','number':original['posting_number'],'posting':original}
        rec = p.Reconciler()
        rec.posting = Mock(side_effect=lambda acc, n, folder, lease: deepcopy(next(x for x in children if x['posting_number'] == n)))
        self.assertEqual(len(rec.family(reg, self.root, Mock())), 2)
        children[1]['status'] = 'awaiting_packaging'
        with self.assertRaisesRegex(p.Deferred, 'waiting_operator'):
            rec.family(reg, self.root, Mock())
        children[1]['status'] = 'awaiting_deliver'
        children[1]['products'][0]['quantity'] = 5
        with self.assertRaisesRegex(p.Deferred, 'quantities_not_conserved'):
            rec.family(reg, self.root, Mock())
        children[1]['order_id'] = 9876
        with self.assertRaisesRegex(p.Deferred, 'order_id_mismatch'):
            rec.family(reg, self.root, Mock())

    def test_processor_keeps_all_three_stages_even_if_one_has_pending_orders(self):
        from ops.fbs_order_processor import run
        runner = Mock(side_effect=[Mock(returncode=0), Mock(returncode=1), Mock(returncode=0)])
        self.assertEqual(run(runner), 1)
        self.assertEqual([call.args[0][2] for call in runner.call_args_list],
                         ['ops.ozon_manual_packages','ops.fbs_confirmed_daily_dispatch','ops.fbs_ready_market_catchup'])

    def test_importer_delegates_registered_child_instead_of_creating_duplicate(self):
        from ops import fbs_ms_import_next5 as importer
        child = posting('12345-0001-3', qty=6)
        child['parent_posting_number'] = '12345-0001-1'
        row = {'platform':'ozon','account':'oz_acc1','number':child['posting_number']}
        with patch.dict(importer.os.environ, {'OZON_CLIENT_ID_ACC1':'test','OZON_API_KEY_ACC1':'test'}), \
             patch.object(importer.requests, 'post', return_value=response({'result':child})), \
             patch.object(p, 'owns', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'owned_by_reconciler'):
                importer._source(row, {})

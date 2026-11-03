# поток: inv
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests
from ops import fbs_ms_post_import_status as status
from ops.fbs_ms_post_import_status import all_available, desired, planned_day, provenance, pos_signature, proof_signature

MSK = ZoneInfo('Europe/Moscow')


class PostImportStatusTests(unittest.TestCase):
    def test_calendar_avoids_november_fourth_holiday(self):
        now = datetime(2026, 11, 2, 16, tzinfo=MSK)
        self.assertEqual(planned_day('Подтвержден', now), '2026-11-03')
        self.assertEqual(planned_day('Неукомплектован', now), '2026-11-05')
        self.assertEqual(planned_day('Собирается', datetime(2026, 11, 3, 12, tzinfo=MSK)), '2026-11-03')
        self.assertEqual(planned_day('Подтвержден', datetime(2026, 11, 5, 15, tzinfo=MSK)), '2026-11-05')
        self.assertEqual(planned_day('Подтвержден', datetime(2026, 11, 5, 16, tzinfo=MSK)), '2026-11-06')

    def test_own_reserve_counts_but_physical_stock_is_required(self):
        position = {'assortment': {'id': 'p'}, 'quantity': 2, 'reserve': 2}
        order = {'positions': {'rows': [position]}}
        self.assertEqual(all_available(order, {'p': {'stock': 2, 'reserve': 2}}, defaultdict(Decimal))[0], True)
        self.assertEqual(all_available(order, {'p': {'stock': 1, 'reserve': 2}}, defaultdict(Decimal))[0], False)
        position['reserve'] = 0
        self.assertEqual(all_available(order, {'p': {'stock': 2, 'reserve': 1}}, defaultdict(Decimal))[0], False)

    def test_shortage_requires_real_price_choice(self):
        priced = {'selected': [{'available': False, 'purchase_kopeks': 43000,
                                'manual_fallback': False, 'tier': 2}]}
        fallback = {'selected': [{'available': False, 'purchase_kopeks': None,
                                  'manual_fallback': True}]}
        self.assertEqual(provenance(priced), (False, True))
        self.assertEqual(provenance(fallback), (True, False))

    def test_already_processed_order_cannot_be_changed(self):
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/product/p'
        order = {'name': '123', 'sum': 100, 'state': {'name': 'Неукомплектован'},
                 'store': {'meta': {'href': 'https://api.moysklad.ru/api/remap/1.2/entity/store/s'}},
                 'positions': {'rows': [{'assortment': {'meta': {'href': href}},
                                         'quantity': 1, 'reserve': 0}]}}
        proof = {'total_kopeks': 100, 'selected': [{'available': False, 'purchase_kopeks': 50}],
                 'payload': {'store': order['store'], 'positions': [
                     {'assortment': {'meta': {'href': href}}, 'quantity': 1}]}}
        target, day, evidence = desired(order, proof, {'p': {'stock': 0, 'reserve': 0}},
                                        defaultdict(Decimal), datetime(2026, 11, 2, 16, tzinfo=MSK))
        self.assertIsNone(target)
        self.assertIsNone(day)
        self.assertEqual(evidence['reason'], 'already_processed_or_payment_pending')

    def test_split_set_with_all_components_on_order_store_next_day_is_confirmed(self):
        root = 'https://api.moysklad.ru/api/remap/1.2/entity/'
        positions = [{'assortment': {'meta': {'href': root + 'product/' + code}},
                      'quantity': 1, 'reserve': 1} for code in ('a', 'b')]
        order = {'name': 'kit-order', 'sum': 90000, 'state': {'name': 'Ожидает подтверждения'},
                 'store': {'meta': {'href': root + 'store/s'}},
                 'positions': {'rows': positions}, 'demands': []}
        proof = {'total_kopeks': 90000,
                 'selected': [{'available': True, 'manual_parent': False, 'card_id': code, 'quantity': '1'}
                              for code in ('a', 'b')],
                 'payload': {'store': order['store'], 'positions': positions}}
        stock = {code: {'stock': 1, 'reserve': 1} for code in ('a', 'b')}
        target, day, _ = desired(order, proof, stock, defaultdict(Decimal),
                                 datetime(2026, 11, 2, 16, tzinfo=MSK))
        self.assertEqual((target, day), ('Подтвержден', '2026-11-03'))

    def test_november_third_cutoff_is_exactly_fifteen_moscow(self):
        for moment, expected in [
                (datetime(2026, 11, 3, 14, 59, 59, tzinfo=MSK), '2026-11-03'),
                (datetime(2026, 11, 3, 15, tzinfo=MSK), '2026-11-05'),
                (datetime(2026, 11, 3, 12, tzinfo=timezone.utc), '2026-11-05')]:
            with self.subTest(moment=moment):
                self.assertEqual(planned_day('Собирается', moment), expected)
                self.assertEqual(planned_day('Неукомплектован', moment), '2026-11-05')

    def test_warehouse_pool_does_not_consume_another_warehouses_stock(self):
        order, _, stock = fixture()
        promised = {('other-store', 'a'): Decimal(1)}
        self.assertTrue(all_available(order, stock, promised)[0])
        promised[('s', 'a')] = Decimal(1)
        self.assertFalse(all_available(order, stock, promised)[0])
        self.assertFalse(all_available(order, stock, {'a': Decimal(1)})[0])

    def test_manual_fallback_is_never_assembled_even_with_saved_full_stock(self):
        order, proof, stock = fixture()
        proof['selected'][0].update(available=True, purchase_kopeks=None,
                                    manual_fallback=True, manual_parent=True)
        self.assertIsNone(desired(order, proof, stock, {}, NOW)[0])
        proof['selected'][0]['available'] = False
        self.assertIsNone(desired(order, proof, stock, {}, NOW)[0])

    def test_readiness_uses_saved_import_proof_without_recalculating_stock(self):
        order, proof, _ = fixture(('a', 'b'))
        self.assertEqual(desired(order, proof, {}, {'a': Decimal(100)}, NOW)[0], 'Собирается')
        proof['selected'][1]['available'] = False
        proof['selected'][1]['purchase_kopeks'] = 43000
        proof['selected'][1]['tier'] = 2
        self.assertEqual(desired(order, proof, {'b': {'stock': 100, 'reserve': 0}}, {}, NOW)[0], 'Неукомплектован')

    def test_import_proof_must_cover_every_product_and_quantity_on_the_order(self):
        order, proof, _ = fixture(('a', 'b'))
        for invalid in ([proof['selected'][0]],
                        [dict(proof['selected'][0], quantity='2'), proof['selected'][1]],
                        [dict(proof['selected'][0], card_id='other'), proof['selected'][1]],
                        [dict(proof['selected'][0], stock_store_id='other'), proof['selected'][1]]):
            with self.subTest(invalid=invalid):
                changed = dict(proof, selected=invalid)
                self.assertIsNone(desired(order, changed, {}, {}, NOW)[0])

    def test_one_missing_set_component_requires_purchase_proof(self):
        order, proof, stock = fixture(('a', 'b'))
        stock['b']['stock'] = 0
        proof['selected'][1].update(available=False, purchase_kopeks=43000, tier=2)
        self.assertEqual(desired(order, proof, stock, {}, NOW)[:2], ('Неукомплектован', '2026-11-05'))
        proof['selected'][1]['purchase_kopeks'] = None
        self.assertEqual(desired(order, proof, stock, {}, NOW)[2]['reason'], 'shortage_without_price_proof')

    def test_shortage_requires_an_approved_supplier_priority(self):
        order, proof, _ = fixture()
        proof['selected'][0].update(available=False, purchase_kopeks=43000)
        for tier in (None, 0, 4, '2', True):
            proof['selected'][0]['tier'] = tier
            self.assertIsNone(desired(order, proof, {}, {}, NOW)[0])
        for tier in (1, 2, 3):
            proof['selected'][0]['tier'] = tier
            self.assertEqual(desired(order, proof, {}, {}, NOW)[0], 'Неукомплектован')

    def test_other_statuses_and_linked_shipments_are_unchanged(self):
        order, proof, stock = fixture()
        for previous in ('Подтвержден', 'Собирается', 'В пути', 'Доставлен', 'Неукомплектован'):
            order['state']['name'] = previous
            self.assertIsNone(desired(order, proof, stock, {}, NOW)[0])
        order['state']['name'] = 'Ожидает подтверждения'
        order['demands'] = [{'meta': {'href': 'existing'}}]
        self.assertEqual(desired(order, proof, stock, {}, NOW)[2]['reason'], 'linked_demand_exists')

    def test_merged_identical_component_lines_preserve_quantity_check(self):
        href = 'https://api.moysklad.ru/api/remap/1.2/entity/product/a'
        proof = {'payload': {'positions': [
            {'assortment': {'meta': {'href': href}}, 'quantity': 1},
            {'assortment': {'meta': {'href': href}}, 'quantity': 1}]}}
        merged = [{'assortment': {'meta': {'href': href}}, 'quantity': 2}]
        self.assertEqual(pos_signature(merged), proof_signature(proof))


NOW = datetime(2026, 11, 3, 14, 59, 59, tzinfo=MSK)


def fixture(codes=('a',)):
    root = status.ms_api.BASE + '/entity/'
    positions = [{'assortment': {'id': code, 'meta': {'href': root + 'product/' + code,
                                                   'type': 'product'}},
                  'quantity': 1, 'reserve': 0, 'price': 90000, 'vat': 5, 'vatEnabled': True}
                 for code in codes]
    order = {'id': 'order-id', 'name': 'order-number', 'sum': 90000 * len(codes),
             'moment': '2026-11-03 10:00:00', 'updated': '2026-11-03 10:00:00',
             'meta': {'uuidHref': 'https://example.invalid/order'},
             'state': {'name': 'Ожидает подтверждения'},
             'store': {'meta': {'href': root + 'store/s'}},
             'positions': {'rows': positions}}
    proof = {'total_kopeks': order['sum'], 'selected': [
                 {'available': True, 'card_id': code, 'quantity': '1',
                  'stock': {'stock': 1, 'reserve': 0}, 'stock_store_id': 's'} for code in codes],
             'payload': {'store': deepcopy(order['store']), 'positions': deepcopy(positions)}}
    return order, proof, {code: {'stock': 1, 'reserve': 0} for code in codes}


class StatusResponseProofTests(unittest.TestCase):
    def apply(self, folder, order, proof, stock, target='Собирается', day='2026-11-03', promised=None):
        after = deepcopy(order)
        after.update(state={'name': target}, deliveryPlannedMoment=day + ' 00:00:00',
                     updated='2026-11-03 14:59:59')
        response = Mock(status_code=200, ok=True)
        response.json.return_value = after
        if promised is None:
            promised = {}
        with patch.object(status, 'RUNTIME', folder), \
                patch.object(status.ms_api, 'get', return_value=deepcopy(order)) as get, \
                patch.object(status, 'stock_for', side_effect=AssertionError('No repeated stock reads')) as stock_read, \
                patch.object(status.requests, 'put', return_value=response) as put, \
                patch.dict(status.os.environ, {'MOYSKLAD_TOKEN': 'test-token'}):
            result = status.apply_one(order, proof, promised, NOW, True)
            stock_read.assert_not_called()
        return result, get, put, promised

    def test_expanded_put_response_is_shipment_proof_without_another_get(self):
        order, proof, stock = fixture(('a', 'b'))
        order['positions']['rows'][0]['reserve'] = 1
        stock['a']['reserve'] = 1
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result, get, put, promised = self.apply(folder, order, proof, stock)
            self.assertEqual(result['to'], 'Собирается')
            self.assertEqual(get.call_count, 1)
            self.assertEqual(put.call_count, 1)
            self.assertEqual(put.call_args.kwargs['params'], {'expand': 'state,store,positions.assortment'})
            readback = json.loads((folder / order['name'] / 'readback.json').read_text())
            self.assertTrue(readback['shipment_due'])
            self.assertEqual(readback['state'], 'Собирается')
            self.assertEqual(readback['positions'], [['a', 1], ['b', 1]])
            self.assertEqual(datetime.fromisoformat(readback['changed_at']).utcoffset().total_seconds(), 0)
            self.assertEqual(promised, {})
            intent = json.loads((folder / order['name'] / 'intent.json').read_text())
            self.assertEqual(intent['decision']['source'], 'import_preflight')
            self.assertEqual(intent['decision']['stock']['a']['snapshots'][0]['stock'],
                             proof['selected'][0]['stock'])

    def test_shortage_has_no_pending_shipment_marker(self):
        order, proof, stock = fixture()
        proof['selected'][0].update(available=False, purchase_kopeks=43000, tier=2)
        stock['a']['stock'] = 0
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            _, get, _, promised = self.apply(folder, order, proof, stock,
                                            target='Неукомплектован', day='2026-11-05')
            readback = json.loads((folder / order['name'] / 'readback.json').read_text())
            self.assertNotIn('shipment_due', readback)
            self.assertNotIn('changed_at', readback)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(promised, {})

    def test_already_processed_fresh_order_does_not_request_stock_or_write(self):
        order, proof, _ = fixture()
        order['state']['name'] = 'Подтвержден'
        with patch.object(status.ms_api, 'get', return_value=order), \
                patch.object(status, 'stock_for') as stock, patch.object(status.requests, 'put') as put:
            result = status.apply_one(order, proof, {}, NOW, True)
        self.assertEqual(result['result'], 'unchanged')
        stock.assert_not_called()
        put.assert_not_called()

    def test_invalid_response_is_not_published_for_the_shipment_worker(self):
        order, proof, stock = fixture()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with self.assertRaisesRegex(RuntimeError, 'status_readback_mismatch'):
                self.apply(folder, order, proof, stock, target='Подтвержден')
            self.assertFalse((folder / order['name'] / 'readback.json').exists())

    def test_timeout_records_one_fact_read_and_never_retries_the_put(self):
        order, proof, stock = fixture()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(status, 'RUNTIME', folder), \
                    patch.object(status.ms_api, 'get', side_effect=[order, order]) as get, \
                    patch.object(status, 'stock_for', return_value=stock), \
                    patch.object(status.requests, 'put', side_effect=requests.Timeout) as put, \
                    patch.dict(status.os.environ, {'MOYSKLAD_TOKEN': 'test-token'}):
                with self.assertRaises(status.StatusPutUncertain):
                    status.apply_one(order, proof, {}, NOW, True)
            self.assertEqual((get.call_count, put.call_count), (2, 1))
            self.assertTrue((folder / order['name'] / 'uncertain.json').exists())
            self.assertFalse((folder / order['name'] / 'readback.json').exists())

    def test_timeout_stops_the_pass_and_handled_orders_are_not_reopened(self):
        order, proof, _ = fixture()
        orders = {number: dict(order, name=number) for number in ('a', 'b', 'handled')}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            handled = folder / 'handled.json'
            handled.write_text(json.dumps({'numbers': ['handled']}))
            with patch.object(status, 'RUNTIME', folder), patch.object(status, 'HANDLED', handled), \
                    patch.object(status, 'LOCK', folder / 'lock'), \
                    patch.object(status, 'imported_proofs', return_value={n: proof for n in orders}), \
                    patch.object(status, 'current_orders', return_value=orders) as current, \
                    patch.object(status, 'apply_one', side_effect=status.StatusPutUncertain('uncertain')) as apply, \
                    patch('builtins.print'):
                summary = status.run(True)
            current.assert_called_once_with(['a', 'b'])
            self.assertEqual(apply.call_count, 1)
            self.assertEqual(len(summary['errors']), 1)


if __name__ == '__main__':
    unittest.main()

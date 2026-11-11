# поток: fin
import copy
import unittest

from ops.warehouse_load_watch import analyze, composition, goods, queue_scenario


def doc(id, qty, *, kind='product', **extra):
    return {'id': id, 'name': id, 'store': {'id': 'e0d74d6f-db92-11ee-0a80-0cda000a40b5'},
            'positions': {'meta': {'size': 1}, 'rows': [
                {'quantity': qty, 'shipped': 0,
                 'assortment': {'id': 'sku', 'name': 'Лазерный картридж', 'meta': {'type': kind}}}]}, **extra}


class WarehouseWatchTest(unittest.TestCase):
    def test_large_order_counted_once_and_extra_is_additive(self):
        s = queue_scenario(45, 5, 900, 1080, large_extra_minutes=4)
        self.assertEqual(s['worker_minutes'], 200)  # 45*4 + 5*4, not 50 orders
        self.assertAlmostEqual(s['coefficient'], 200/540)

    def test_deadline_and_unmeasured_extra(self):
        s = queue_scenario(20, 10, 1080, 1080)
        self.assertIsNone(s['coefficient'])
        self.assertTrue(s['deadline_passed'])
        self.assertFalse(s['large_adjustment_calibrated'])
        self.assertEqual(s['worker_minutes'], 80)

    def test_no_dimension_inference_or_cross_sku_join(self):
        dimensions = {'different': {'cm': [80, 20, 20]}}
        c = composition([doc('order', 2)], dimensions)[0]
        self.assertFalse(c['large'])
        self.assertEqual(c['laser_unknown_ids'], ['sku'])
        self.assertFalse(c['bundle_flag'])

    def test_large_skus_in_same_order_not_multiple_orders(self):
        d = doc('order', 2)
        d['positions']['rows'].append(copy.deepcopy(d['positions']['rows'][0]))
        d['positions']['meta']['size'] = 2
        c = composition([d], {'sku': {'cm': [40, 10, 10]}})
        self.assertEqual(sum(bool(x['large']) for x in c), 1)
        self.assertEqual(c[0]['units'], 4)

    def test_incomplete_page_and_services(self):
        d = doc('order', 1)
        d['positions']['meta']['size'] = 2
        with self.assertRaises(ValueError): goods(d)
        self.assertEqual(goods(doc('service', 50, kind='service')), [])

    def test_dedup_linked_receipts_partial_purchase_and_independent_supply(self):
        p = doc('po', 10, agent={'id': 'supplier'}, applicable=True)
        p['positions']['rows'][0]['shipped'] = 3
        linked = doc('linked', 7, applicable=False, purchaseOrder={'id': 'po'},
                     agent={'id': 'supplier'}, moment='2026-11-10 08:00:00')
        independent = doc('independent', 5, applicable=False,
                          agent={'id': 'supplier'}, moment='2026-11-10 08:00:00')
        snap = {'target_date': '2026-11-10', 'started_msk': 'now', 'finished_msk': 'now',
                'purchaseorder': [p], 'supply': [linked, independent], 'linked_supplies': [linked],
                'customerorder': [doc('o', 2, state={'name': 'Неукомплектован'})]}
        hist = {'receipts': [], 'days': [{'receipt_units': 100}]}
        a = analyze(snap, hist, {})
        self.assertEqual(a['incoming_units'], 12)  # 7 outstanding + 5 unlinked, not 19
        self.assertEqual(a['composition']['incomplete_orders'], 1)
        self.assertFalse(a['odyssey_model']['available'])

    def test_invalid_workforce_or_negative_adjustment(self):
        for kwargs in ({'workers': 0}, {'large_extra_minutes': -1}, {'other_worker_minutes': -1}):
            with self.assertRaises(ValueError): queue_scenario(10, 1, 900, 1080, **kwargs)


if __name__ == '__main__': unittest.main()

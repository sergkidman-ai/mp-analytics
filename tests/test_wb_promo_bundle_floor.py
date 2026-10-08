# поток: mkt
import unittest

from ops.wb_promo_guard import bundle_multiplier, decide, min_net, unit_cost


class BundleFloorTests(unittest.TestCase):
    def test_bundle_multiplier_accepts_wb_suffixes(self):
        self.assertEqual(bundle_multiplier('3933X3TQSKD9'), 3)
        self.assertEqual(bundle_multiplier('3933X12K6OWK'), 12)
        self.assertEqual(bundle_multiplier('3933X6'), 6)
        self.assertEqual(bundle_multiplier('3933', '3933X9I7CAH1'), 9)
        self.assertEqual(bundle_multiplier('3933X123TQ'), 1)

    def test_eligible_supplier_price_wins_over_stale_low_stock_snapshot(self):
        stock = {'3933': {'qty_own': 0, 'qty_remote': 0, 'cost_own': None,
                          'supplier_min': 213, 'tc_price': 228}}
        self.assertEqual(unit_cost('3933', {}, stock, need=6),
                         (213, 'supplier_min', 'remote'))

    def test_last_buy_fallback_still_requires_supplier_stock(self):
        stock = {'3933': {'qty_own': 0, 'qty_remote': 4, 'cost_own': None,
                          'supplier_min': None, 'tc_price': 228}}
        self.assertEqual(unit_cost('3933', {}, stock, need=6),
                         (228, 'tc_last_buy', 'remote'))

    def test_minimum_per_piece_and_percentage(self):
        self.assertEqual(min_net(1000), 300)
        self.assertEqual(min_net(6000, 6), 1800)
        self.assertEqual(min_net(30000, 6), 3000)
        self.assertEqual(min_net(9000, 9), 2700)

    def test_bundle_below_new_floor_is_raised(self):
        row = decide('wb_acc1', {1: (20000, 50, 10000, '0004X6')},
                     {1: {'cogs': 3600, 'bundle': 6}}, {1: 15000}, .569)[0]
        self.assertEqual(row['floor_net'], 1800)
        self.assertEqual(row['status'], 'todo')
        self.assertGreaterEqual(row['target_price'], (3600 + 1800) / .431)

    def test_untouched_bundle_is_not_enrolled(self):
        row = decide('wb_acc1', {1: (10000, 0, 10000, '0004X6')},
                     {1: {'cogs': 3600, 'bundle': 6}}, {1: 10000}, .569)[0]
        self.assertEqual(row['status'], 'skip')


if __name__ == '__main__':
    unittest.main()

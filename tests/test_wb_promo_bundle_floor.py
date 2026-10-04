# поток: mkt
import unittest

from ops.wb_promo_guard import decide, min_net


class BundleFloorTests(unittest.TestCase):
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

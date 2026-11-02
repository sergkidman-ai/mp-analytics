# поток: inv
from decimal import Decimal
import unittest

from ops.fbs_ms_import_policy import AllocationPool, Card, SelectionError, choose_bundle, choose_bundle_plan, choose_card


ODYSSEY_WB = '61d01266-2fe1-11f0-0a80-0f7f000bf967'
ODYSSEY = '7225fcf1-10f7-11ea-0a80-06380004eeda'
BLOSSOM = '141a0940-99f7-11e7-7a34-5acf0005e7dc'


def card(id, code='0019', supplier=ODYSSEY, brand='at', price=10000,
         stock=0, reserve=0):
    return Card(id, code, supplier, brand, price, Decimal(stock), Decimal(reserve))


class PolicyTests(unittest.TestCase):
    def test_uses_available_stock_after_other_reserves(self):
        stocked = card('a', stock=3, reserve=2)
        empty = card('b', stock=0, price=100)
        self.assertEqual(choose_card([stocked, empty], '0019', Decimal(1)), (stocked, True))
        self.assertEqual(choose_card([stocked, empty], '0019', Decimal(2)), (empty, False))

    def test_earliest_supplier_tier_then_lowest_current_price(self):
        first_expensive = card('a', supplier=ODYSSEY_WB, brand='wb', price=20000)
        first_cheap = card('b', supplier=ODYSSEY_WB, brand='wb', price=15000)
        later_cheap = card('c', supplier=BLOSSOM, price=100)
        self.assertEqual(choose_card([later_cheap, first_expensive, first_cheap], '0019', 1),
                         (first_cheap, False))

    def test_wrong_special_brand_is_regular_tier(self):
        special_wrong_brand = card('a', supplier=ODYSSEY_WB, brand='at', price=100)
        regular = card('b', supplier=ODYSSEY, price=200)
        self.assertEqual(choose_card([regular, special_wrong_brand], '0019', 1),
                         (special_wrong_brand, False))

    def test_missing_exact_card_or_price_blocks(self):
        with self.assertRaisesRegex(SelectionError, 'exact_product_card_missing'):
            choose_card([card('a')], '0020', 1)
        with self.assertRaisesRegex(SelectionError, 'current_purchase_price_missing'):
            choose_card([card('a', price=None)], '0019', 1)

    def test_bundle_prefers_one_pair_and_rejects_three(self):
        cards = [card('a', '0019', ODYSSEY_WB, 'wb', stock=2),
                 card('b', '0020', ODYSSEY_WB, 'wb', stock=2),
                 card('c', '0019', ODYSSEY, 'at', stock=2)]
        plan = choose_bundle([('0019', 1), ('0020', 1)], cards)
        self.assertEqual([c.id for c, _ in plan], ['a', 'b'])
        three = [card(str(i), str(i), ODYSSEY, str(i), stock=1) for i in range(3)]
        with self.assertRaisesRegex(SelectionError, 'bundle_exceeds_two'):
            choose_bundle([(str(i), 1) for i in range(3)], three)

    def test_bundle_multiplies_repeated_component_and_checks_stock(self):
        with self.assertRaisesRegex(SelectionError, 'bundle_component_unavailable'):
            choose_bundle([('0019', 2), ('0019', 2)], [card('a', stock=3)])

    def test_bundle_plan_prefers_complete_own_stock_and_one_supplier_brand(self):
        own = [card('a', '0019', ODYSSEY_WB, 'wb', stock=1),
               card('b', '0020', ODYSSEY_WB, 'wb', stock=1),
               card('c', '0020', ODYSSEY, 'at', stock=10)]
        remote = [card('a', '0019', ODYSSEY_WB, 'wb', price=100, stock=20),
                  card('b', '0020', ODYSSEY_WB, 'wb', price=100, stock=20)]
        plan = choose_bundle_plan([('0019', 1), ('0020', 1)], own, remote)
        self.assertEqual([(c.id, available) for c, _, available in plan], [('a', True), ('b', True)])

    def test_bundle_plan_uses_remote_price_within_supplier_tier(self):
        own = [card('a', '0019', ODYSSEY_WB, 'wb', stock=0),
               card('b', '0020', ODYSSEY_WB, 'wb', stock=0),
               card('c', '0020', ODYSSEY_WB, 'wb', stock=0)]
        remote = [card('a', '0019', ODYSSEY_WB, 'wb', price=200, stock=4),
                  card('b', '0020', ODYSSEY_WB, 'wb', price=300, stock=4),
                  card('c', '0020', ODYSSEY_WB, 'wb', price=100, stock=4)]
        plan = choose_bundle_plan([('0019', 1), ('0020', 1)], own, remote)
        self.assertEqual([(c.id, available) for c, _, available in plan], [('a', False), ('c', False)])

    def test_bundle_plan_uses_more_order_warehouse_stock_before_one_pair(self):
        own = [card('a', '0019', ODYSSEY_WB, 'wb', stock=1),
               card('b', '0020', ODYSSEY, 'at', stock=1),
               card('c', '0030', ODYSSEY, 'at', stock=0)]
        remote = [card('a', '0019', ODYSSEY_WB, 'wb', price=100, stock=10),
                  card('d', '0020', ODYSSEY_WB, 'wb', price=100, stock=10),
                  card('e', '0030', ODYSSEY_WB, 'wb', price=100, stock=10)]
        plan = choose_bundle_plan([('0019', 1), ('0020', 1), ('0030', 1)], own, remote)
        self.assertEqual([(c.id, available) for c, _, available in plan],
                         [('a', True), ('b', True), ('e', False)])

    def test_bundle_plan_splits_repeated_component_between_own_and_remote(self):
        own = [card('a', '0019', ODYSSEY_WB, 'wb', stock=1)]
        remote = [card('a', '0019', ODYSSEY_WB, 'wb', price=150, stock=1)]
        plan = choose_bundle_plan([('0019', 2)], own, remote)
        self.assertEqual([(c.id, q, available) for c, q, available in plan],
                         [('a', 1, True), ('a', 1, False)])

    def test_bundle_plan_splits_component_across_two_suppliers(self):
        own = [card('a', '0019', ODYSSEY_WB, 'wb', stock=1),
               card('b', '0019', ODYSSEY, 'at', stock=1)]
        plan = choose_bundle_plan([('0019', 2)], own, [])
        self.assertEqual(sum(q for _, q, _ in plan), 2)
        self.assertEqual({c.supplier_id for c, _, _ in plan}, {ODYSSEY_WB, ODYSSEY})

    def test_bundle_plan_rejects_three_pairs_or_missing_remote_stock(self):
        cards = [card(str(i), str(i), ODYSSEY, str(i), stock=1) for i in range(3)]
        with self.assertRaisesRegex(SelectionError, 'bundle_exceeds_two'):
            choose_bundle_plan([(str(i), 1) for i in range(3)], cards, cards)
        with self.assertRaisesRegex(SelectionError, 'bundle_component_unavailable'):
            choose_bundle_plan([('0019', 1)], [card('a', stock=0)], [card('a', stock=0)])

    def test_pool_does_not_promise_same_stock_twice(self):
        pool = AllocationPool()
        cards = [card('a', stock=1), card('b', price=50)]
        self.assertEqual(pool.choose(cards, '0019', 1), (cards[0], True))
        self.assertEqual(pool.choose(cards, '0019', 1), (cards[1], False))
        self.assertEqual(pool.promised['a'], 1)


if __name__ == '__main__':
    unittest.main()

# поток: inv
from decimal import Decimal
from unittest import TestCase

from ops.fbs_ms_order_payload import Line, line_total
from ops.fbs_ms_set_prices import split_kit_price


class SetPriceTests(TestCase):
    def test_gross_discount_and_net_are_preserved_for_multiple_kits(self):
        for quantity, price, discount, units in ((1, 189800, '0', 4),
                                                  (2, 629600, '12.5', 6),
                                                  (3, 381615, '23.75', 12)):
            with self.subTest(quantity=quantity, discount=discount):
                cards = ['p' + str(i) for i in range(units * quantity)]
                lines = split_kit_price(cards, quantity, price, Decimal(discount))
                self.assertEqual(sum(x.price_kopeks for x in lines), price * quantity)
                self.assertEqual(sum(line_total(x) for x in lines),
                                 line_total(Line('parent', Decimal(quantity), price,
                                                 Decimal(discount))))
                self.assertTrue(all(x.discount_percent == Decimal(discount) for x in lines))

    def test_unallocatable_kit_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, 'set_price_allocation_invalid'):
            split_kit_price(['a', 'b'], 1, 1, Decimal(0))

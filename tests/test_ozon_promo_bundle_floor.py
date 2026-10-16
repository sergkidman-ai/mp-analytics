# поток: mkt
import unittest
from datetime import date
from unittest.mock import patch

from ops import ozon_promo_guard as guard
from ops import ozon_stock_action as stock
from ops.wb_promo_guard import min_net


class OzonBundleFloorTests(unittest.TestCase):
    def test_quantity_excludes_colour_sets_and_random_suffixes(self):
        for code, expected in [('3933X6', 6), ('3933х6', 6), ('3216X0V1FNDJ', 1),
                               ('3933X0', 1), ('1588X99JKQEZ', 1), ('3216', 1)]:
            with self.subTest(code=code):
                self.assertEqual(stock.bundle_quantity(code), expected)

    def test_floor_matches_wb_per_piece_and_percentage(self):
        for cogs, quantity, expected_profit in [(1200, 6, 1800), (40000, 6, 4000),
                                               (1200, 1, 300)]:
            with self.subTest(cogs=cogs, quantity=quantity):
                expected = (cogs + expected_profit) / .4
                self.assertAlmostEqual(stock.floor_price(cogs, .4, quantity), expected)
                self.assertEqual(min_net(cogs, quantity), expected_profit)

    def test_ordinary_guard_raises_to_bundle_floor(self):
        row = dict(offer_id='3933X6', cogs=1200, cogs_source='supplier_min_x6',
                   keep_ratio=.4, action_price=4000, max_price=8000)
        guard.decide(row)
        self.assertEqual((row['mode'], row['floor_price'], row['new_price']),
                         ('raise', 7500, 7500))

    def test_ordinary_guard_removes_when_new_floor_exceeds_cap(self):
        row = dict(offer_id='3933X6', cogs=1200, cogs_source='supplier_min_x6',
                   keep_ratio=.4, action_price=4000, max_price=6000)
        guard.decide(row)
        self.assertEqual(row['mode'], 'remove')
        self.assertIsNone(row['new_price'])

    def test_ladder_uses_bundle_floor_for_stock_and_regional(self):
        for undercut in (False, True):
            with self.subTest(undercut=undercut):
                row = dict(offer_id='3933X6', cogs=1200, cap=10000,
                           inside=False, now_price=None)
                stock._decide(row, .4, {'3933X6': {'rung': stock.STEPS}}, {},
                              date(2026, 10, 8), undercut=undercut)
                self.assertEqual((row['floor'], row['price']), (7500, 7500))

    def test_ladder_never_lifts_promo_cap_to_new_floor(self):
        row = dict(offer_id='3933X6', cogs=1200, cap=6000, inside=True, now_price=4000)
        stock._decide(row, .4, {}, {}, date(2026, 10, 8), undercut=False)
        self.assertEqual(row['cap'], 6000)
        self.assertIsNone(row['price'])
        self.assertIn('пол', row['skip'])

    def costs(self, offers, shipments=(), links=(), sets=(), buys=()):
        def rows(sql, params=None):
            if 'from prc_tc_link' in sql:
                return list(links)
            if 'from ms_product' in sql:
                return list(shipments)
            if 'from set_cost' in sql:
                return list(sets)
            if 'from tc_buy_price_latest' in sql:
                return list(buys)
            self.fail(f'Unexpected SQL: {sql}')
        with patch.object(stock.db, 'query', side_effect=rows):
            return stock.cogs_map(offers)

    def shipment(self, code, unit, status='delivered'):
        return dict(ec=code, unit=unit, status=status, dt=date(2026, 8, 8))

    def test_parent_shipment_multiplied_but_exact_bundle_not_multiplied_twice(self):
        cases = [([self.shipment('1367', 206.58)], 413.16),
                 ([self.shipment('1367X2', 700), self.shipment('1367', 206.58)], 700)]
        for shipments, expected in cases:
            with self.subTest(shipments=shipments):
                value, _ = self.costs(['1367X2'], shipments=shipments)['1367X2']
                self.assertAlmostEqual(value, expected)

    def test_exact_return_is_preferred_and_not_multiplied_twice(self):
        value, source = self.costs(['1367X2'], shipments=[
            self.shipment('1367X2', 700), self.shipment('1367X2', 650, 'return_stock'),
            self.shipment('1367', 206.58)])['1367X2']
        self.assertEqual(value, 650)
        self.assertIn('возврат', source)

    def test_parent_purchase_and_set_cost_are_multiplied(self):
        for source in ('buys', 'sets'):
            field = 'buy_price' if source == 'buys' else 'cost'
            value, _ = self.costs(['3933X6'], **{
                source: [dict(external_code='3933', **{field: 228})]})['3933X6']
            self.assertEqual(value, 1368)

    def test_exact_purchase_and_set_cost_are_already_full(self):
        for source in ('buys', 'sets'):
            field = 'buy_price' if source == 'buys' else 'cost'
            value, _ = self.costs(['3933X6'], **{
                source: [dict(external_code='3933X6', **{field: 1368})]})['3933X6']
            self.assertEqual(value, 1368)

    def test_universal_model_cost_is_scaled_to_target_quantity(self):
        for reference, reference_cost in [('1367', 200), ('1367X2', 400)]:
            value, _ = self.costs(['3933X6'], links=[dict(ec='3933', rc=reference)],
                                  shipments=[self.shipment(reference, reference_cost)])['3933X6']
            self.assertEqual(value, 1200)

    def test_single_card_cost_and_missing_cost_unchanged(self):
        result = self.costs(['1367', '1367RANDOM', '3933X6'],
                            shipments=[self.shipment('1367', 206.58)])
        self.assertEqual(result['1367'][0], 206.58)
        self.assertEqual(result['1367RANDOM'][0], 206.58)
        self.assertEqual(result['3933X6'], (None, 'НЕТ'))


if __name__ == '__main__':
    unittest.main()

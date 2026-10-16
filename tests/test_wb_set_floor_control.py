# поток: mkt
import unittest
from unittest.mock import patch
from ops import wb_promo_guard as guard


def stock_record(price=None, tc=None):
    return dict(qty_own=0, qty_remote=10 if price else 0, cost_own=None,
                supplier_min=price, tc_price=tc)


class SetFloorControlTests(unittest.TestCase):
    def costs(self, mapped='2860', vendor='4297Y8RW996R', comps=None, tc=53689, own_cost=None):
        comps = comps or {'2860': 1, '2861': 1}
        stock = {'4297': stock_record(tc=tc), '2860': stock_record(1690.47),
                 '2861': stock_record(1357)}
        if own_cost is not None:
            stock['4297'].update(qty_own=1, cost_own=own_cost)
        with patch.object(guard, 'sets_map', return_value={'4297': comps}), \
             patch.object(guard, 'links_map', return_value={}), \
             patch.object(guard, 'stock_map', return_value=stock), \
             patch.object(guard.db, 'query', side_effect=[
                 [{'nm_id': 1, 'vendor_code': vendor, 'external_code': mapped}],
                 [{'nm_id': 1, 'vendor_code': vendor, 'title': 'Картриджи DS №727'}]]):
            return guard.cogs_map('wb_acc2', {1: (208851, 92, 16708.08, vendor)})[1]

    def test_shipment_component_cannot_replace_set_identity(self):
        c = self.costs()
        self.assertEqual(c['external_code'], '4297')
        self.assertEqual(c['mapped_external_code'], '2860')
        self.assertTrue(c['is_set'])
        self.assertEqual(c['cogs'], 53689)
        self.assertEqual(c['source'], 'set_own_price')

    def test_abnormally_low_own_set_cost_uses_full_purchase_price(self):
        self.assertEqual(self.costs(own_cost=1)['cogs'], 53689)

    def test_valid_own_set_cost_is_retained(self):
        self.assertEqual(self.costs(own_cost=32000)['cogs'], 32000)
        self.assertEqual(self.costs(own_cost=32000)['source'], 'set_own_stock')

    def test_unrelated_shipment_code_cannot_replace_known_set(self):
        self.assertEqual(self.costs(mapped='9999')['cogs'], 53689)

    def test_set_price_must_not_be_below_component_total(self):
        self.assertAlmostEqual(self.costs(tc=1000)['cogs'], 3047.47)
        self.assertEqual(self.costs(tc=1000)['source'], 'set_components')

    def test_repeated_single_component_is_still_a_set(self):
        c = self.costs(comps={'2860': 3}, tc=None)
        self.assertTrue(c['is_set'])
        self.assertAlmostEqual(c['cogs'], 1690.47 * 3)

    def test_missing_component_cost_does_not_create_partial_cost(self):
        c = self.costs(comps={'2860': 1, '9999': 1}, tc=None)
        self.assertIsNone(c['cogs'])

    def test_discounted_set_is_controlled_without_export(self):
        goods = {1: (208851, 92, 16708.08, '4297Y8RW996R')}
        costs = {1: dict(cogs=53689, bundle=1, is_set=True)}
        with patch.object(guard.db, 'query', side_effect=[[], []]):
            control = guard.floor_controlled_bundles('wb_acc2', goods, costs,
                active_promo_names=['Текущая акция'])
        row = guard.decide('wb_acc2', goods, costs, {1: 16708.08}, .554,
                           floor_controlled=control)[0]
        self.assertEqual(row['status'], 'todo')
        self.assertAlmostEqual(row['floor_price'], 132416.82, places=2)
        self.assertEqual(row['send_disc'], 36)
        self.assertGreaterEqual(row['send_price'] * .64, row['floor_price'])

    def test_no_active_calendar_does_not_expand_scope_by_discount(self):
        with patch.object(guard.db, 'query', side_effect=[[], []]):
            scope = guard.floor_controlled_bundles('wb_acc2',
                {1: (10000, 50, 5000, '4297')},
                {1: dict(cogs=53689, bundle=1, is_set=True)})
        self.assertEqual(scope, {})

    def test_no_discount_does_not_expand_scope_without_export(self):
        with patch.object(guard.db, 'query', side_effect=[[], []]):
            scope = guard.floor_controlled_bundles('wb_acc2',
                {1: (10000, 0, 10000, '4297')},
                {1: dict(cogs=53689, bundle=1, is_set=True)},
                active_promo_names=['Текущая акция'])
        self.assertEqual(scope, {})


if __name__ == '__main__':
    unittest.main()


class SetMappingTests(unittest.TestCase):
    def test_mapping_keeps_set_parent_over_shipment_component(self):
        from reports import margin_control
        with patch.object(margin_control.db, 'query', side_effect=[
            [{'nm_id': 1, 'vc': '4297Y8RW996R'}, {'nm_id': 2, 'vc': '1111AAA'}],
            [], [{'nm': 1, 'ec': '2860'}, {'nm': 2, 'ec': '2222'}],
            [{'external_code': '4297'}]]):
            result = margin_control._mapping('wb_acc2', {'4297','2860','1111','2222'})
        self.assertEqual(result[1], ('4297', 'set_vendor'))
        self.assertEqual(result[2], ('2222', 'shipment'))

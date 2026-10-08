# поток: mkt
import unittest
from datetime import date
from unittest.mock import patch

from ops import ozon_promo_guard as guard, ozon_stock_action as stock
from ops import wb_bundle_plan as bundles, wb_plan_raise as raise_plan


class BundleDetectionTests(unittest.TestCase):
    def test_both_accounts_use_title_confirmed_suffix(self):
        for account, code in [('oz_acc1', '3933X6'), ('oz_acc2', '3933X6HHWDS8')]:
            with self.subTest(account=account):
                row = dict(account=account, offer_id=code, name='Комплект лент (6 шт)',
                           cogs=1200, cogs_source='supplier_min_x6', keep_ratio=.4,
                           action_price=4000, max_price=8000)
                guard.decide(row)
                self.assertEqual((row['bundle_qty'], row['floor_price']), (6, 7500))
                row = dict(offer_id=code, name='Комплект лент (6 шт)', cogs=1200,
                           cap=10000, inside=False, now_price=None)
                stock._decide(row, .4, {}, {}, date(2026, 10, 8), undercut=False)
                self.assertEqual((row['bundle_qty'], row['floor']), (6, 7500))

    def test_colour_sets_and_random_suffixes_are_not_bundles(self):
        for code, title, field in [('3216X0V1FNDJ', 'Картриджи DS TN-421', 4),
                                   ('4692X5BV7567', 'Картриджи DS №963', 4),
                                   ('0916X1TWMYE3', 'Картриджи DS T0870-T0879', 8),
                                   ('1588X99JKQEZ', 'Картридж DS TNP-27K', 1)]:
            with self.subTest(code=code):
                self.assertEqual(stock.bundle_quantity(code, title), 1)
                self.assertFalse(bundles.is_bundle({'vendorCode': code, 'title': title,
                    'characteristics': [{'id': 179792, 'value': [str(field)]}]}))

    def test_suffix_requires_matching_number_and_explicit_bundle_title(self):
        for title in ('Комплект лент (3 шт)', 'Картриджи (6 шт)', ''):
            self.assertEqual(stock.bundle_quantity('3933X6HHWDS8', title), 1)
        self.assertEqual(stock.bundle_quantity('3933X6HHWDS8', 'Комплект лент (6 шт)'), 6)

    def test_full_name_not_truncated_before_quantity(self):
        name = 'Комплект картриджей ' + 'A' * 70 + ' (12 шт)'
        with patch.object(stock, '_req', return_value={'items': [dict(
                id=1, offer_id='3933X12K6OWK', name=name)]}):
            offer, title = stock.offer_of('oz_acc2', [1])[1]
        self.assertEqual(title, name)
        self.assertEqual(stock.bundle_quantity(offer, title), 12)

    def test_ozon_cost_uses_same_validated_quantity_as_floor(self):
        code = '3933X6HHWDS8'
        with patch('ops.wb_promo_guard.sets_map', return_value={}), \
             patch('ops.wb_promo_guard.links_map', return_value={}), \
             patch('ops.wb_promo_guard.stock_map', return_value={}), \
             patch('ops.wb_promo_guard.unit_cost', return_value=(200, 'supplier_min', 'remote')) as unit:
            costs = guard.cost_map([code, '3216X0'], quantities={code: 6, '3216X0': 1})
        self.assertEqual(costs[code], (1200, 'supplier_min_x6'))
        self.assertEqual(costs['3216X0'], (200, 'supplier_min'))
        self.assertEqual(unit.call_args_list[0].kwargs['need'], 6)

    def test_ordinary_snapshot_passes_validated_quantity_for_both_accounts(self):
        for account, code in [('oz_acc1', '3933X6'), ('oz_acc2', '3933X6HHWDS8')]:
            with self.subTest(account=account), \
                 patch.object(stock, 'keep_ratio', return_value=(.4, 'test')), \
                 patch.object(stock, 'actions', return_value=[dict(
                     id=1, title='Автоакция', is_participating=True)]), \
                 patch.object(stock, 'participants', return_value=[dict(
                     id=1, action_price=4000, max_action_price=8000)]), \
                 patch.object(stock, 'offer_of', return_value={1: (code, 'Комплект лент (6 шт)')}), \
                 patch.object(guard, 'cost_map', return_value={code: (1200, 'supplier_min_x6')}) as costs:
                rows, *_ = guard.snapshot(account)
            self.assertEqual(costs.call_args.kwargs['quantities'], {code: 6})
            guard.decide(rows[0])
            self.assertEqual(rows[0]['floor_price'], 7500)

    def test_stock_parent_cost_multiplies_title_confirmed_suffix(self):
        def query(sql, params=None):
            if 'from ms_product' in sql:
                return [dict(ec='3933', unit=200, dt=date(2026, 10, 8), status='delivered')]
            return []
        with patch.object(stock.db, 'query', side_effect=query):
            value, _ = stock.cogs_map(['3933X6HHWDS8'], titles={
                '3933X6HHWDS8': 'Комплект лент (6 шт)'})['3933X6HHWDS8']
        self.assertEqual(value, 1200)

    def test_wb_plan_raise_rejects_cap_below_suffix_bundle_floor(self):
        row = dict(nm_id=1, cap=4000, promos=1, vendor_code='3933X6HHWDS8',
                   base=10000, disc=70, now=3000)
        with patch.object(raise_plan.db, 'query', return_value=[row]), \
             patch.object(raise_plan.w, 'cogs_map', return_value={1: dict(
                 cogs=1200, source='supplier_min_x6', bundle=6)}) as cost:
            self.assertEqual(raise_plan.candidates('wb_acc2'), [])
        self.assertEqual(cost.call_args.args, ('wb_acc2',))
        self.assertEqual(cost.call_args.kwargs['goods'][1][3], '3933X6HHWDS8')


if __name__ == '__main__':
    unittest.main()

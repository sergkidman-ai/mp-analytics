# поток: mkt
import unittest
from datetime import date
from unittest.mock import patch

from ops import wb_promo_guard as guard


class WbFloorControlTests(unittest.TestCase):
    def test_real_cards_with_low_baseline_are_raised_to_full_floor(self):
        for nm, code, base, price, cost, expected in [
            (1557822564, '0644X3J34F76', 28220, 7055, 4544.1, 12206.50),
            (1557847390, '0686X3LH0K17', 15279, 3819.75, 1683, 5791.48),
        ]:
            with self.subTest(code=code):
                row = guard.decide('wb_acc2', {nm: (base, 75, price, code)},
                    {nm: {'cogs': cost, 'bundle': 3}}, {nm: price}, .554,
                    floor_controlled={nm: 'участник по выгрузке и цене'})[0]
                self.assertEqual(row['status'], 'todo')
                self.assertAlmostEqual(row['floor_price'], expected, places=2)
                self.assertGreaterEqual(row['send_price'] * (1-row['send_disc']/100), expected)

    def test_previous_confirmed_bundle_is_protected_after_small_drop(self):
        row = guard.decide('wb_acc2', {1: (9894, 50, 4947, '0587X6DAMKZ4')},
            {1: {'cogs': 501.48, 'bundle': 6}}, {1: 5162}, .554,
            floor_controlled={1: 'пол ранее подтверждён сторожем'})[0]
        self.assertEqual(row['status'], 'todo')
        self.assertGreater(row['target_price'], 4947)

    def test_cost_increase_without_price_drop_rechecks_floor(self):
        row = guard.decide('wb_acc2', {1: (16524, 25, 12393, '0948X12LDDNM')},
            {1: {'cogs': 1936.08, 'bundle': 12}}, {1: 12393}, .554,
            floor_controlled={1: 'пол ранее подтверждён сторожем'})[0]
        self.assertEqual(row['status'], 'todo')
        self.assertEqual(row['floor_net'], 3600)

    def test_missing_baseline_does_not_disable_controlled_bundle(self):
        row = guard.decide('wb_acc1', {1: (10000, 50, 5000, '3933X6')},
            {1: {'cogs': 1200, 'bundle': 6}}, {}, .569,
            floor_controlled={1: 'пол ранее подтверждён сторожем'})[0]
        self.assertEqual(row['status'], 'todo')

    def test_bundle_at_floor_and_single_card_still_skipped(self):
        for quantity, price, cost in [(6, 8000, 1200), (1, 500, 1200)]:
            row = guard.decide('wb_acc1', {1: (10000, 50, price, '3933')},
                {1: {'cogs': cost, 'bundle': quantity}}, {1: price}, .569,
                floor_controlled={1: 'участник по выгрузке и цене'})[0]
            self.assertEqual(row['status'], 'skip')

    def test_unknown_cost_keeps_existing_restore_policy(self):
        row = guard.decide('wb_acc1', {1: (10000, 50, 5000, '3933X6')},
            {1: {'cogs': None, 'bundle': 6}}, {1: 5000}, .569,
            floor_controlled={1: 'пол ранее подтверждён сторожем'})[0]
        self.assertEqual(row['status'], 'skip')

    def test_scope_uses_promo_price_or_previous_confirmed_floor_and_account(self):
        goods = {1: (10000, 50, 5000, '3933X6'), 2: (10000, 50, 5000, '3933X6'),
                 3: (10000, 50, 5000, '3933X6'), 4: (10000, 50, 5000, '3933')}
        costs = {nm: {'cogs': 1200, 'bundle': (1 if nm == 4 else 6)} for nm in goods}
        with patch.object(guard.db, 'query', side_effect=[
                [{'nm_id': 1, 'plan_price': 5000, 'promo_name': 'Осенние скидки автоматические скидки'},
                 {'nm_id': 2, 'plan_price': 4000, 'promo_name': 'Осенние скидки автоматические скидки'}],
                [{'nm_id': 3}]]) as query:
            scope = guard.floor_controlled_bundles('wb_acc2', goods, costs, date(2026, 10, 9),
                active_promo_names=['Осенние скидки (автоматические скидки)'])
        self.assertEqual(set(scope), {1, 3})
        for call in query.call_args_list:
            self.assertEqual(call.args[1][0], 'wb_acc2')
            self.assertEqual(call.args[1][1], [1, 2, 3])

    def test_finished_promo_is_not_an_extra_floor_control_source(self):
        goods = {1: (10000, 50, 5000, '3933X6')}
        costs = {1: {'cogs': 1200, 'bundle': 6}}
        with patch.object(guard.db, 'query', side_effect=[
                [{'nm_id': 1, 'plan_price': 6000, 'promo_name': 'Законченная акция'}], []]):
            scope = guard.floor_controlled_bundles('wb_acc2', goods, costs,
                active_promo_names=['Осенние скидки'])
        self.assertEqual(scope, {})

    def test_no_eligible_bundles_does_not_query_database(self):
        with patch.object(guard.db, 'query') as query:
            self.assertEqual(guard.floor_controlled_bundles('wb_acc1', {}, {}), {})
        query.assert_not_called()


if __name__ == '__main__':
    unittest.main()

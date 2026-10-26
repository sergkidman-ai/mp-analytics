# поток: mkt
from argparse import Namespace
import unittest
from unittest.mock import patch

from ops import ozon_promo_guard as guard
from ops import ozon_stock_action as oz


def row(action_id, maximum, *, product_id=1, seller=70, future=False, limit=70):
    return dict(account='oz_acc1', action_id=action_id, action_title=str(action_id),
                action_type='STOCK_DISCOUNT', product_id=product_id, offer_id=str(product_id),
                name='товар', action_price=float(seller), action_price_limit=float(limit),
                max_price=float(maximum), cogs=20, cogs_source='test', keep_ratio='0.5',
                stock=1, min_stock=0, is_current=not future, is_future=future,
                status='dry', note=None)


class SharedPriceTests(unittest.TestCase):
    def test_snapshot_marks_scheduled_action_as_future(self):
        action = dict(id=30, title='Будущая акция', action_type='STOCK_DISCOUNT',
                      is_participating=True, date_start='2099-10-30T00:00:00Z',
                      date_end='2099-10-31T00:00:00Z')
        product = dict(id=1, action_price=146, action_price_limit=72,
                       max_action_price=72, stock=1, min_stock=0)
        with patch.object(oz, 'keep_ratio', return_value=(.5, 'test')), \
             patch.object(oz, 'actions', return_value=[action]), \
             patch.object(oz, 'is_whitelisted', return_value=False), \
             patch.object(oz, 'participants', return_value=[product]), \
             patch.object(oz, 'offer_of', return_value={1: ('1', 'товар')}), \
             patch.object(oz, 'bundle_quantity', return_value=1), \
             patch.object(guard, 'cost_map', return_value={'1': (20, 'test')}):
            rows, _, _, _ = guard.snapshot('oz_acc1')
        self.assertTrue(rows[0]['is_future'])
        self.assertFalse(rows[0]['is_current'])
        self.assertEqual(rows[0]['action_price_limit'], 72)

    def test_targeted_cap_read_uses_product_filter(self):
        with patch.object(oz, '_req', return_value={'items': [
                {'product_id': 1, 'price': {'price': '4147'}}]}) as request:
            self.assertEqual(guard.read_shared_caps('oz_acc1', [1]), {1: 4147.0})
        self.assertEqual(request.call_args.args[2], '/v5/product/info/prices')
        self.assertEqual(request.call_args.args[3]['filter']['product_id'], [1])

    def plan(self):
        rows = [row(10, 100), row(20, 75)]
        with patch.object(oz, 'floor_price', return_value=80), \
             patch.object(oz, 'bundle_quantity', return_value=1):
            for r in rows:
                guard.decide(r)
        return guard.coordinate_shared_prices(rows)

    def test_future_limit_below_floor_is_visible_without_price_write(self):
        future = row(30, 72, seller=146, future=True, limit=72)
        with patch.object(oz, 'floor_price', return_value=87), \
             patch.object(oz, 'bundle_quantity', return_value=1):
            guard.decide(future)
        self.assertEqual(future['mode'], 'watch_future')
        self.assertIsNone(future['new_price'])
        with patch.object(oz, 'update_products') as update, \
             patch.object(oz, 'remove_products') as remove:
            guard.execute('oz_acc1', [future])
        update.assert_not_called()
        remove.assert_not_called()

    def test_future_warning_distinguishes_expected_exit_from_low_price(self):
        future = row(30, 72, seller=146, future=True, limit=72)
        rows = []
        args = Namespace(apply=False, no_log=True, limit=None)
        with patch.object(guard, 'snapshot', return_value=(
                [future], [dict(id=30)], .5, 'test')), \
             patch.object(oz, 'floor_price', return_value=87), \
             patch.object(oz, 'bundle_quantity', return_value=1), \
             patch.object(guard, 'read_shared_caps', return_value={1: 146}):
            guard._run_account('oz_acc1', args, [], rows)
        self.assertEqual(rows[0]['mode'], 'watch_future')
        self.assertEqual(rows[0]['future_expectation'], 'exit_if_cap_unchanged')

    def test_one_update_covers_two_actions_and_verifies_both(self):
        rows = self.plan()
        self.assertEqual([(r['mode'], r['shared_price']) for r in rows],
                         [('raise', 80), ('remove', 80)])
        with patch.object(oz, 'update_products', return_value=dict(
                active={1}, inactive=set(), rejected={}, warnings=[])) as update, \
             patch.object(oz, 'participants', side_effect=[
                 [dict(id=1, action_price=80)], []]) as participants, \
             patch.object(guard, 'read_shared_caps', return_value={1: 80}), \
             patch.object(guard.time, 'sleep'):
            guard.execute('oz_acc1', rows)
        update.assert_called_once()
        self.assertEqual(update.call_args.args[1], 10)
        self.assertEqual(update.call_args.args[2][0]['action_price'], 80)
        self.assertEqual(participants.call_count, 2)
        self.assertEqual([r['status'] for r in rows], ['confirmed', 'confirmed'])

    def test_limit_never_splits_one_product_across_actions(self):
        rows = self.plan()
        other = row(40, 100, product_id=2, seller=75)
        with patch.object(oz, 'floor_price', return_value=80), \
             patch.object(oz, 'bundle_quantity', return_value=1):
            guard.decide(other)
        guard._cut(rows + [other], 1)
        self.assertEqual([r['mode'] for r in rows], ['raise', 'remove'])
        self.assertEqual(other['mode'], 'ok_deferred')

    def test_inconsistent_current_prices_are_held_for_review(self):
        rows = self.plan()
        rows[1]['action_price'] = 71
        guard.coordinate_shared_prices(rows)
        self.assertEqual(rows[0]['mode'], 'review_shared')
        self.assertEqual(rows[1]['mode'], 'review_shared')
        with patch.object(oz, 'update_products') as update, \
             patch.object(oz, 'remove_products') as remove:
            guard.execute('oz_acc1', rows)
        update.assert_not_called()
        remove.assert_not_called()

    def test_unknown_cost_never_lowers_current_price_to_exit_actions(self):
        rows = [row(10, 100, seller=150), row(20, 80, seller=150)]
        for r in rows:
            r['cogs'] = None
            r['cogs_source'] = 'unknown'
            guard.decide(r)
        guard.coordinate_shared_prices(rows)
        self.assertEqual([r['shared_price'] for r in rows], [151, 151])
        self.assertEqual([r['mode'] for r in rows], ['remove', 'remove'])

    def test_rejected_shared_raise_gets_one_shared_exclusion_retry(self):
        rows = self.plan()
        answers = [dict(active=set(), inactive=set(), rejected={1: 'discount'}, warnings=[]),
                   dict(active=set(), inactive={1}, rejected={}, warnings=[])]
        with patch.object(oz, 'update_products', side_effect=answers) as update, \
             patch.object(oz, 'participants', side_effect=[[], []]) as participants, \
             patch.object(guard, 'read_shared_caps', return_value={1: 101}), \
             patch.object(guard.time, 'sleep'):
            guard.execute('oz_acc1', rows)
        self.assertEqual(update.call_count, 2)
        self.assertEqual([c.args[2][0]['action_price'] for c in update.call_args_list], [80, 101])
        self.assertEqual(participants.call_count, 2)
        self.assertEqual([r['mode'] for r in rows], ['remove', 'remove'])
        self.assertEqual([r['status'] for r in rows], ['confirmed', 'confirmed'])

    def test_safe_price_in_unexpected_remaining_action_does_not_force_exclusion(self):
        rows = self.plan()
        with patch.object(oz, 'update_products', return_value=dict(
                active={1}, inactive=set(), rejected={}, warnings=[])) as update, \
             patch.object(oz, 'participants', side_effect=[
                 [dict(id=1, action_price=80)], [dict(id=1, action_price=80)]]), \
             patch.object(guard, 'read_shared_caps', return_value={1: 80}), \
             patch.object(guard.time, 'sleep'):
            guard.execute('oz_acc1', rows)
        update.assert_called_once()
        self.assertEqual([r['status'] for r in rows], ['confirmed', 'error'])
        self.assertFalse(rows[1].get('below_floor_verified'))

    def test_shared_update_timeout_does_not_trigger_blind_second_write(self):
        rows = self.plan()
        with patch.object(oz, 'update_products', side_effect=TimeoutError('timeout')) as update, \
             patch.object(oz, 'participants') as participants:
            guard.execute('oz_acc1', rows)
        update.assert_called_once()
        participants.assert_not_called()
        self.assertEqual([r['status'] for r in rows], ['error', 'error'])

    def test_different_unified_cap_reports_conflict_without_second_write(self):
        rows = self.plan()
        with patch.object(oz, 'update_products', return_value=dict(
                active={1}, inactive=set(), rejected={}, warnings=[])) as update, \
             patch.object(oz, 'participants', side_effect=[
                 [dict(id=1, action_price=70)], [dict(id=1, action_price=70)]]), \
             patch.object(guard, 'read_shared_caps', return_value={1: 70}), \
             patch.object(guard.time, 'sleep'):
            guard.execute('oz_acc1', rows)
        update.assert_called_once()
        self.assertTrue(all(r['shared_cap_mismatch'] for r in rows))
        self.assertEqual([r['status'] for r in rows], ['error', 'error'])


if __name__ == '__main__':
    unittest.main()

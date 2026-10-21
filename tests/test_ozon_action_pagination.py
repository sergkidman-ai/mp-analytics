# поток: fin
"""Observed Ozon EOF/count mismatches must never become false removal proofs."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ops import ozon_actions as api
from ops import ozon_promo_guard as guard
from ops import ozon_stock_action as stock
from tests.test_ozon_actions_v2 import product


class PaginationTests(unittest.TestCase):
    def partial(self):
        return [dict(products=[product(1)], total=3, last_id='one'),
                dict(products=[], total=3, last_id='one')]

    def test_recorded_empty_page_same_cursor_is_explicit_partial(self):
        result = api.products(Mock(side_effect=self.partial()), 'oz_acc2', 4375943, allow_partial=True)
        self.assertEqual([r['id'] for r in result], [1])
        self.assertFalse(result.complete)
        self.assertEqual(result.diagnostic['termination'], 'empty_page_same_cursor')
        self.assertEqual(result.diagnostic['reported_totals'], [3])
        self.assertEqual(result.diagnostic['pages'], 2)

    def test_default_stock_and_other_consumers_still_reject_partial_absence(self):
        with patch.object(stock, '_req', side_effect=self.partial()):
            with self.assertRaises(api.ActionPaginationIncomplete) as cm:
                stock.participants('oz_acc2', 4375943)
        self.assertEqual(len(cm.exception.listing), 1)
        self.assertIn('action=4375943', str(cm.exception))

    def test_underreported_total_does_not_truncate_remaining_pages(self):
        req = Mock(side_effect=[dict(products=[product(1)], total=1, last_id='one'),
                                dict(products=[product(2)], total=1, last_id='two'),
                                dict(products=[], total=1, last_id='two')])
        result = api.products(req, 'oz_acc2', 5, allow_partial=True)
        self.assertEqual(len(result), 2)
        self.assertFalse(result.complete)
        self.assertEqual(req.call_count, 3)

    def test_matching_total_is_complete_after_cursor_exhaustion(self):
        req = Mock(side_effect=[dict(products=[product(1)], total=1, last_id='one'),
                                dict(products=[], total=1, last_id='one')])
        result = api.products(req, 'oz_acc2', 5)
        self.assertTrue(result.complete)
        self.assertEqual(req.call_count, 2)

    def test_empty_page_with_advancing_cursor_is_not_eof(self):
        req = Mock(side_effect=[dict(products=[], total=1, last_id='skip'),
                                dict(products=[product(1)], total=1, last_id='')])
        self.assertEqual(len(api.products(req, 'oz_acc2', 5)), 1)
        self.assertEqual(req.call_count, 2)

    def test_repeated_nonempty_or_duplicate_page_is_not_accepted_as_partial(self):
        for second in (product(1), product(2)):
            req = Mock(side_effect=[dict(products=[product(1)], total=3, last_id='one'),
                                    dict(products=[second], total=3, last_id='one')])
            with self.subTest(second=second['id']), self.assertRaises(api.ActionProtocolError):
                api.products(req, 'oz_acc2', 5, allow_partial=True)

    def test_coverage_reports_changed_total_even_when_final_count_matches(self):
        req = Mock(side_effect=[dict(products=[product(1)], total=3, last_id='one'),
                                dict(products=[product(2)], total=2, last_id='')])
        result = api.products(req, 'oz_acc2', 5, allow_partial=True)
        self.assertFalse(result.complete)
        self.assertEqual(result.diagnostic['reported_totals'], [2, 3])

    def test_page_budget_is_bounded_and_never_returns_silent_partial(self):
        with self.assertRaises(api.ActionProtocolError):
            api.products(Mock(return_value=dict(products=[product(1)], total=3, last_id='one')),
                         'oz_acc2', 5, max_pages=1, allow_partial=True)

    def test_bad_count_and_invalid_limits_fail_closed(self):
        for total in (-1, True, 'unknown', 1.5):
            with self.subTest(total=total), self.assertRaises(api.ActionProtocolError):
                api.products(Mock(return_value=dict(products=[], total=total, last_id='')), 'oz_acc2', 5)
        with self.assertRaises(ValueError):
            api.products(Mock(), 'oz_acc2', 5, limit=101)


class GuardCoverageTests(unittest.TestCase):
    def setUp(self):
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def row(self, mode):
        return dict(action_id=5, product_id=1, mode=mode, status='sent', new_price=100,
                    reason='test')

    def partial(self, rows):
        return api.ActionProducts(rows, complete=False, diagnostic=dict(complete=False, received=len(rows), reported_totals=[10]))

    def test_missing_removal_in_partial_reread_stays_unconfirmed(self):
        row = self.row('remove'); issues = []
        with patch.object(stock, 'participants', return_value=self.partial([])), patch.object(guard.time, 'sleep'):
            guard.verify('oz_acc2', [row], issues=issues)
        self.assertEqual(row['status'], 'sent')
        self.assertIn('отсутствие не подтверждено', row['note'])
        self.assertEqual(issues[0]['stage'], 'verify')

    def test_missing_raise_is_not_misclassified_as_successful_removal(self):
        row = self.row('raise')
        with patch.object(stock, 'participants', return_value=self.partial([])), patch.object(guard.time, 'sleep'):
            guard.verify('oz_acc2', [row], issues=[])
        self.assertEqual((row['mode'], row['status']), ('raise', 'sent'))
        self.assertFalse(row.get('verification_failed'))

    def test_positive_price_proof_can_confirm_raise_in_partial_list(self):
        row = self.row('raise')
        with patch.object(stock, 'participants', return_value=self.partial([dict(id=1, action_price=101)])), patch.object(guard.time, 'sleep'):
            guard.verify('oz_acc2', [row], issues=[])
        self.assertEqual(row['status'], 'confirmed')

    def test_positive_presence_disproves_removal_even_in_partial_list(self):
        row = self.row('remove')
        with patch.object(stock, 'participants', return_value=self.partial([dict(id=1, action_price=101)])), patch.object(guard.time, 'sleep'):
            guard.verify('oz_acc2', [row], issues=[])
        self.assertEqual(row['status'], 'error')

    def test_failed_action_does_not_hide_other_actions(self):
        acts = [dict(id=i, title='Maximum', is_participating=True) for i in (5, 6)]
        issues = []
        with patch.object(stock, 'actions', return_value=acts), \
             patch.object(stock, 'keep_ratio', return_value=(.4, 'basis')), \
             patch.object(stock, 'participants', side_effect=[RuntimeError('page unavailable'), self.partial([dict(id=1, action_price=70, max_action_price=100)])]), \
             patch.object(stock, 'offer_of', return_value={1: ('0447', 'ink')}), \
             patch.object(guard, 'cost_map', return_value={'0447': (71.68, 'supplier_min')}):
            rows, actions, _, _ = guard.snapshot('oz_acc2', issues=issues)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(actions), 2)
        self.assertEqual(len(issues), 2)
        self.assertEqual(rows[0]['action_id'], 6)

    def test_main_continues_second_account_and_does_not_write_or_notify_in_dry_run(self):
        def run(acc, args, issues, rows):
            if acc == 'oz_acc1':raise RuntimeError('first cabinet failed')
            return 'second cabinet completed'
        with patch('sys.argv', ['guard', '--no-log', '--no-tg']), \
             patch.object(guard, '_run_account', side_effect=run) as calls, \
             patch.object(stock, 'send') as send, patch.object(guard, 'save') as save, \
             patch.object(Path, 'open', side_effect=AssertionError('no run journal in dry/no-log')):
            with self.assertRaises(SystemExit) as cm:guard.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual([c.args[0] for c in calls.call_args_list], ['oz_acc1', 'oz_acc2'])
        send.assert_not_called();save.assert_not_called()

    def test_run_journal_exposes_incomplete_and_successful_empty_account(self):
        from types import SimpleNamespace
        args = SimpleNamespace(apply=False, wave='test', no_log=False)
        with tempfile.TemporaryDirectory() as tmp, patch.object(guard, 'BASE_DIR', Path(tmp)):
            self.assertEqual(guard._save_run('oz_acc1', args, [], [dict(stage='snapshot', action_id=5)]), 'partial')
            self.assertEqual(guard._save_run('oz_acc2', args, [], []), 'ok')
            records = [json.loads(s) for s in (Path(tmp)/'logs/oz_promo_guard_runs.jsonl').read_text().splitlines()]
        self.assertEqual([r['status'] for r in records], ['partial', 'ok'])


if __name__ == '__main__':
    unittest.main()

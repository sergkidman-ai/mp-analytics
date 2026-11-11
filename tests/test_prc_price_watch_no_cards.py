# поток: prc
"""Дневной сторож может обновлять остатки без массовой перезаписи карточек."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from ops import prc_price_watch as watcher


class ReceiptOnlyWatchTests(unittest.TestCase):
    def test_receipts_only_forwards_no_cards_and_keeps_apply(self):
        with patch('prices.run.main', return_value=0) as run:
            _, code = watcher.run_load(SimpleNamespace(key='colortek'), False, no_cards=True)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], ['--supplier', 'colortek', '--apply', '--no-cards'])

    def test_unprocessed_mail_does_not_receive_price_only_option(self):
        with patch.object(watcher.unprocessed, 'main', return_value=0) as run:
            _, code = watcher.run_load(SimpleNamespace(key='bulat', unprocessed=True), True, no_cards=True)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], ['--supplier', 'bulat'])

    def test_receipts_only_never_runs_automatic_card_creation(self):
        with patch('prices.auto_card.run') as create:
            result = watcher.auto_cards(SimpleNamespace(key='colortek'), False, 0, None, no_cards=True)
        self.assertIsNone(result)
        create.assert_not_called()

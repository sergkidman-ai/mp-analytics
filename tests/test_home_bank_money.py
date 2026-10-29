# поток: fin
"""Главная суммирует только счета с известным опорным остатком."""
import datetime as dt
import unittest
from unittest.mock import patch

from web.app import _bank_money


class HomeBankMoneyTest(unittest.TestCase):
    def test_unanchored_account_does_not_inflate_total(self):
        class FakeBalances:
            def __init__(self, org):
                self.api = {('alfa', '1001'): [(dt.date(2026, 10, 28), 100.25)]}
                self.manual = {}
                self.last_txn = {('alfa', '1001'): dt.date(2026, 10, 28),
                                 ('ozon', '2002'): dt.date(2026, 10, 28)}

            def accounts(self):
                return {('alfa', '1001'), ('ozon', '2002')}

            def at(self, key, day):
                return (100.25, True) if key == ('alfa', '1001') else (9000, False)

            def kind(self, key):
                return 'api' if key == ('alfa', '1001') else 'roll'

        with patch('web.cashflow_api.Balances', FakeBalances):
            result = _bank_money('7807355364')
        self.assertEqual(result['total'], 100.25)
        self.assertEqual(len(result['accounts']), 1)
        self.assertEqual(result['accounts'][0]['as_of'], '2026-10-28')
        self.assertEqual(result['missing'][0]['account_last4'], '2002')


if __name__ == '__main__':
    unittest.main()

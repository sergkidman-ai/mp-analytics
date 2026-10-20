# поток: fin
"""Analytics Plus: денежная классификация, фикс-плата и независимый сторож кодов."""
import contextlib
import datetime as dt
import io
import unittest
from decimal import Decimal
from unittest.mock import patch

from collectors import ozon_accrual_adapter as adapter
from ops import fin_charge_watch as watch
from reports import ozon_mp_report as report


def accrual(account_id=1, type_id=127, amount='-28000'):
    return {'accrual_id': account_id, 'date': '2026-10-01',
            'accrued_category': 'NON_ITEM', 'unit_number': '30.09.26-30.10.26',
            'total_amount': {'amount': amount, 'currency': 'RUB'},
            'non_item_fee': {'type_id': type_id,
                             'accrued': {'amount': amount, 'currency': 'RUB'}}}


class AnalyticsPlusAccounting(unittest.TestCase):
    def test_adapter_uses_explicit_service_type(self):
        operation = adapter.build(accrual(), None, {})
        self.assertEqual(operation['operation_type'], 'OperationSubscriptionAnalyticsPlus')
        self.assertEqual(operation['operation_type_name'], 'Аналитика Plus')
        self.assertEqual(operation['type'], 'services')
        self.assertEqual(operation['amount'], -28000)
        self.assertEqual(operation['services'], [])

    def test_balance_keeps_total_and_separates_fixed_subscription(self):
        operations = [adapter.build(accrual(), None, {}),
                      adapter.build(accrual(2, 52, '-6990'), None, {}),
                      adapter.build(accrual(3, 99999, '-250'), None, {})]
        with patch.object(report.db, 'query', return_value=[{'payload': x} for x in operations]):
            balance, fixed, details, fixed_codes = report._balance_range(
                'oz_acc1', '2026-10-01', '2026-11-01')
        self.assertEqual(balance['promo'], 34990)
        self.assertEqual(balance['unclassified'], 250)
        self.assertEqual(balance['promo'] + balance['unclassified'], 35240)
        self.assertEqual(fixed, 34990)
        self.assertEqual(sum(details['promo'].values()), balance['promo'])
        self.assertEqual(fixed_codes['OP:OperationSubscriptionAnalyticsPlus'], 28000)

    def test_reversal_keeps_its_sign(self):
        operation = adapter.build(accrual(amount='28000'), None, {})
        with patch.object(report.db, 'query', return_value=[{'payload': operation}]):
            balance, _, details, _ = report._balance_range(
                'oz_acc1', '2026-10-01', '2026-11-01')
        self.assertEqual(balance['promo'], -28000)
        self.assertEqual(details['promo']['OP:OperationSubscriptionAnalyticsPlus'], -28000)


class ChargeWatchAnalyticsPlus(unittest.TestCase):
    def scanned(self):
        rows = [{'account': account, 'accrual_date': dt.date(2026, 10, 1),
                 'payload': accrual(i)}
                for i, account in enumerate(('oz_acc1', 'oz_acc2'), 1)]
        seen = {}
        with patch.object(watch.db, 'query', return_value=rows):
            watch.scan_ozon(seen)
        return seen

    def test_scans_both_accounts_even_without_dictionary_entry(self):
        with patch.object(watch, 'TYPES', {}):
            seen = self.scanned()
        item = seen[('ozon', 'начисление', '127')]
        self.assertEqual(item['label'], 'Тип 127')
        self.assertEqual(item['amount'], Decimal('56000'))
        self.assertEqual(item['rows'], 2)
        self.assertEqual(len(item['accounts']), 2)

    def test_new_code_is_reported_and_seeded_code_is_suppressed(self):
        seen = self.scanned()
        baseline = [{'platform': 'ozon', 'kind': 'начисление', 'code': '52'}]
        for known, expected in ((baseline, 1),
                                (baseline + [{'platform': 'ozon', 'kind': 'начисление',
                                              'code': '127'}], 0)):
            with self.subTest(expected=expected), \
                    patch.object(watch, 'scan', return_value=seen), \
                    patch.object(watch.db, 'query', return_value=known), \
                    patch.object(watch, 'source_health', return_value=[]), \
                    patch.object(watch, '_send') as send, \
                    patch.object(watch, '_save') as save, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(watch.run(today=dt.date(2026, 10, 20)), expected)
                send.assert_not_called()
                save.assert_not_called()


if __name__ == '__main__':
    unittest.main()

# поток: fin
"""Income details must preserve signs, the accounting total and closed history."""
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from reports import ozon_mp_report as report
from reports import ozon_mp_freeze as freeze


class OtherIncomeDetails(unittest.TestCase):
    def test_residual_income_is_not_sale_or_expense_and_reversal_is_preserved(self):
        operations = [
            {'payload': {'operation_type': 'MarketplaceSellerCorrectionOperation',
                         'amount': 285, 'accruals_for_sale': 200, 'sale_commission': -10,
                         'services': [{'name': 'MarketplaceServiceItemDirectFlowLogistic', 'price': -5}]}},
            {'payload': {'operation_type': 'MarketplaceSellerCorrectionOperation', 'amount': -30}},
            {'payload': {'operation_type': 'MarketplaceSellerReexposureDeliveryReturnOperation', 'amount': 50}},
        ]
        with patch.object(report.db, 'query', return_value=operations):
            balance, _, detail, _ = report._balance_range('oz_acc1', '2026-10-01', '2026-11-01')
        self.assertEqual(balance['other'], 120)
        self.assertEqual(detail['other']['OP:MarketplaceSellerCorrectionOperation'], 70)
        self.assertEqual(detail['other']['OP:MarketplaceSellerReexposureDeliveryReturnOperation'], 50)
        self.assertEqual(sum(detail['other'].values()), balance['other'])
        self.assertEqual(balance['delivery'], 5)
        self.assertEqual(detail['delivery']['MarketplaceServiceItemDirectFlowLogistic'], 5)
        self.assertEqual(report._derive(balance, 1, 0, 0)['payout'], 305)

    def test_negative_income_is_displayed_as_deduction(self):
        key = report.det_key('other', 'MarketplaceSellerReexposureDeliveryReturnOperation')
        self.assertEqual(report._kind(key), 'inflow')
        self.assertEqual(report._fmt(key, -20), '−20')
        self.assertEqual(report._fmt(key, 20), '20')

    def test_refresh_does_not_erase_verified_legacy_allocation(self):
        hist = {'period_keys': ['2026-01'], 'accounts': {'oz_acc1': {
            'lines': {'other': [100]}, 'detail': {'other': {
                'OP:MarketplaceSellerReexposureDeliveryReturnOperation': [20],
                'HIST:RealizationReportCorrection': [80]}}}}}
        raw = {'other': {'OP:MarketplaceSellerReexposureDeliveryReturnOperation': 20}}
        with patch.object(freeze, '_lock', return_value=nullcontext()), \
                patch.object(freeze, '_load', return_value=hist), \
                patch.object(freeze, 'ACCOUNTS', ('oz_acc1',)), \
                patch.object(freeze.R, 'balance_detail', return_value=raw), \
                patch.object(freeze, '_save_atomic') as save:
            self.assertEqual(freeze.refresh_detail(['2026-01']), [])
        save.assert_not_called()
        self.assertEqual(hist['accounts']['oz_acc1']['detail']['other']['HIST:RealizationReportCorrection'], [80])


if __name__ == '__main__':
    unittest.main()

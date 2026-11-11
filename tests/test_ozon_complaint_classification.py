# поток: fin
"""Moving complaints between expense lines must preserve payout and profit."""
import unittest
from unittest.mock import patch

from reports import ozon_mp_report as report


class ComplaintClassification(unittest.TestCase):
    def test_complaints_are_separate_from_positive_service_correction(self):
        operations = [
            {'payload': {'operation_type': 'AccrualType_RealizationReportCorrection',
                         'amount': 18325.99}},
            {'payload': {'operation_type': 'AccrualType_DefectFineComplaint',
                         'amount': -644.32}},
        ]
        old_registry = {k: v for k, v in report.RESID_OPS.items()
                        if k != 'AccrualType_DefectFineComplaint'}
        with patch.object(report.db, 'query', return_value=operations):
            with patch.object(report, 'RESID_OPS', old_registry):
                before, _, _, _ = report._balance_range('oz_acc1', '2026-10-01', '2026-11-01')
            after, _, detail, _ = report._balance_range('oz_acc1', '2026-10-01', '2026-11-01')
        self.assertAlmostEqual(before['unclassified'], -17681.67)
        self.assertAlmostEqual(after['unclassified'], -18325.99)
        self.assertAlmostEqual(after['penalty'], 644.32)
        self.assertAlmostEqual(detail['penalty']['OP:AccrualType_DefectFineComplaint'], 644.32)
        self.assertEqual(report.det_label('OP:AccrualType_DefectFineComplaint'), 'Жалобы покупателей')
        for key in ('itog', 'payout', 'net'):
            self.assertAlmostEqual(report._derive(before, 1, 0, 100)[key],
                                   report._derive(after, 1, 0, 100)[key])

    def test_reversed_complaint_keeps_its_direction(self):
        operations = [{'payload': {'operation_type': 'AccrualType_DefectFineComplaint',
                                   'amount': 120.25}}]
        with patch.object(report.db, 'query', return_value=operations):
            after, _, detail, _ = report._balance_range('oz_acc1', '2026-10-01', '2026-11-01')
        self.assertAlmostEqual(after['penalty'], -120.25)
        self.assertAlmostEqual(detail['penalty']['OP:AccrualType_DefectFineComplaint'], -120.25)
        self.assertEqual(after['unclassified'], 0)


if __name__ == '__main__':
    unittest.main()

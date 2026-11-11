# поток: fin
"""An empty recovered database must not produce financial conclusions."""
import unittest
from unittest.mock import patch

from web.app import business, weekly_business, signals, _optional_accounting_cogs


class EmptyBIHistoryTest(unittest.TestCase):
    def setUp(self):
        # Tests model a genuinely empty installation, independent of live manifests.
        recovery = patch('reports.recovered_finance.state', return_value=None)
        recovery.start()
        self.addCleanup(recovery.stop)

    @patch('reports.archived_finance.periods', return_value=[])
    @patch('web.app.db.query', return_value=[{'p': None}])
    def test_business_marks_financial_values_unknown(self, query, periods):
        result = business()
        self.assertIs(result['data_available'], False)
        self.assertIsNone(result['total']['net'])
        self.assertIsNone(result['total']['revenue'])
        self.assertIsNone(result['net_after_opex'])
        self.assertEqual(query.call_count, 1)

    @patch('web.app.db.query', return_value=[{'p': None}])
    def test_weekly_does_not_parse_a_missing_period(self, query):
        self.assertEqual(weekly_business()['rows'], [])
        self.assertEqual(query.call_count, 1)

    @patch('web.app.db.query', return_value=[{'p': None}])
    def test_signals_do_not_claim_the_business_is_healthy(self, query):
        result = signals()
        self.assertIsNone(result['to_target'])
        self.assertTrue(result['tips'])
        self.assertTrue(all(tip['sev'] == 'info' for tip in result['tips']))
        self.assertEqual(query.call_count, 1)

    @patch('web.app.db.query', return_value=[{'available': False}])
    def test_missing_optional_accounting_import_is_unknown(self, query):
        self.assertIsNone(_optional_accounting_cogs('2026-10-01'))
        self.assertEqual(query.call_count, 1)

    @patch('web.app.db.query', side_effect=[[{'available': True}], [{'c': 123.45}]])
    def test_existing_accounting_comparison_is_preserved(self, query):
        self.assertEqual(_optional_accounting_cogs('2026-10-01'), 123.45)
        self.assertEqual(query.call_args.args[1], ('2026-10-01',))


if __name__ == '__main__':
    unittest.main()

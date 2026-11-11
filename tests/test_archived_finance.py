# поток: fin
import unittest
from unittest.mock import patch

from reports import archived_finance as archive


def snapshot():
    return {'period_keys': ['2026-09'], 'provisional': ['2026-09'],
            'accounts': {'wb_acc1': {'lines': {'own_price': [1000], 'sales': [900],
                 'returns': [0], 'to_pay': [800], 'delivery': [50]},
                 'cogs': [300], 'net': [451], 'orders': [2], 'returns_cnt': [0]}}}


class ArchivedFinanceTest(unittest.TestCase):
    @patch.object(archive.wb_mp_report, '_hist', side_effect=snapshot)
    def test_preserves_saved_cells_and_marks_rounding_and_provisional(self, hist):
        row=archive.account('wb','wb_acc1','2026-09-01')
        self.assertEqual(row['net'],451)
        self.assertEqual(row['cogs'],300)
        self.assertEqual(row['payout'],750)
        self.assertEqual(row['rounding_delta'],-1)
        self.assertTrue(row['provisional'])
        self.assertFalse(row['details_restored'])

    @patch.object(archive.wb_mp_report, '_hist', side_effect=snapshot)
    def test_absent_month_is_not_zero(self, hist):
        self.assertIsNone(archive.account('wb','wb_acc1','2026-10-01'))

    def test_missing_cogs_rejects_account_instead_of_guessing(self):
        h=snapshot();h['accounts']['wb_acc1']['cogs']=[None]
        with patch.object(archive.wb_mp_report,'_hist',return_value=h):
            self.assertIsNone(archive.account('wb','wb_acc1','2026-09-01'))

    def test_company_selection_never_includes_the_other_account(self):
        with patch.object(archive,'periods',return_value=['2026-09']),patch.object(archive,'account')as account:
            account.return_value=None
            self.assertIsNone(archive.business('2026-09-01','7811803918'))
            self.assertEqual(account.call_args_list[0].args[:2],('wb','wb_acc2'))
            self.assertEqual(account.call_args_list[1].args[:2],('ozon','oz_acc2'))
            self.assertEqual(account.call_count,2)

    def test_actual_preserved_reports_match_their_saved_totals(self):
        count=0
        for platform,module in archive.MODULES.items():
            h=module._hist()
            for name,record in h['accounts'].items():
                for i,month in enumerate(h['period_keys']):
                    row=archive.account(platform,name,month)
                    self.assertIsNotNone(row)
                    self.assertEqual(row['net'],record['net'][i])
                    self.assertEqual(row['cogs'],record['cogs'][i])
                    count+=1
        self.assertGreater(count,40)


if __name__=='__main__':unittest.main()

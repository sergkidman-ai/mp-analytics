# поток: fin
import datetime as dt
import unittest
from web.turnover_api import _assemble_royalty, _digital_royalty


class RoyaltyTurnoverTest(unittest.TestCase):
    def test_brand_scope_returns_and_missing_data(self):
        rows = [
            ('ozon', [
                dict(account='oz_acc1', ym='2026-07', brand='Цифровой квадрат', sales=100, rets=12, row_count=1),
                dict(account='oz_acc1', ym='2026-07', brand='Картридж орг', sales=500, rets=40, row_count=1),
                dict(account='oz_acc1', ym='2026-07', brand=None, sales=9, rets=1, row_count=2),
                dict(account='oz_acc2', ym='2026-07', brand='Dsquare', sales=250, rets=25, row_count=1),
                dict(account='oz_acc2', ym='2026-07', brand='ДС принт', sales=300, rets=0, row_count=1),
            ]),
            ('wb', [dict(account='wb_acc1', ym='2026-07', sales=30, rets=2)]),
            ('yandex', [dict(account='ya_acc1', ym='2026-07', sales=20, rets=0)]),
        ]
        d = _assemble_royalty(dt.date(2026, 10, 5), rows, [])
        self.assertEqual([m['ym'] for m in d['months']], ['2026-07', '2026-08', '2026-09', '2026-10'])
        a, b = d['months'][0]['orgs'].values()
        self.assertEqual(a['net'], 136)
        self.assertEqual(a['base'], 150)
        self.assertEqual(a['platforms']['ozon']['base'], 100)
        self.assertEqual(d['basis'], 'sales_before_returns')
        self.assertEqual(a['ozon_excluded']['net'], 460)
        self.assertEqual(a['ozon_unclassified']['net'], 8)
        self.assertEqual(a['ozon_unclassified']['rows'], 2)
        self.assertEqual(b['net'], 225)
        self.assertIsNone(b['platforms']['yandex'])
        self.assertIsNone(d['months'][-1]['orgs']['7807355364']['platforms']['ozon'])
        self.assertIsNone(d['months'][-1]['orgs']['7807355364']['net'])

    def test_known_report_without_target_brand_is_zero(self):
        d = _assemble_royalty(dt.date(2026, 7, 5), [('ozon', [
            dict(account='oz_acc2', ym='2026-07', brand='ДС принт', sales=10, rets=0, row_count=1)
        ])], [])
        self.assertEqual(d['months'][0]['orgs']['7811803918']['platforms']['ozon']['net'], 0)

    def test_money_rounding(self):
        d = _assemble_royalty(dt.date(2026, 7, 5), [('wb', [
            dict(account='wb_acc1', ym='2026-07', sales=100.12, rets=10.11)
        ])], [])
        self.assertEqual(d['months'][0]['orgs']['7807355364']['net'], 90.01)


    def test_monthly_rate_thresholds_and_whole_base(self):
        cases = [(0, 2.25), (4999999.99, 2.25), (5000000, 2.15),
                 (9999999.99, 2.15), (10000000, 2.0), (19999999.99, 2.0),
                 (20000000, 1.75), (30000000, 1.75), (30000000.01, 1.5)]
        for base, rate in cases:
            with self.subTest(base=base):
                self.assertEqual(_digital_royalty(base)['rate_percent'], rate)
        self.assertEqual(_digital_royalty(6000000)['amount'], 129000)
        self.assertEqual(_digital_royalty(150)['amount'], 3.38)
        self.assertIsNone(_digital_royalty(None)['amount'])

    def test_monthly_sum_diskver_and_no_estimated_missing_month(self):
        d = _assemble_royalty(dt.date(2026, 10, 5), [('wb', [
            dict(account='wb_acc1', ym='2026-07', sales=6000000, rets=100000),
            dict(account='wb_acc1', ym='2026-08', sales=4000000, rets=50000),
        ])], [])
        july, august, september, october = d['months']
        self.assertEqual(july['orgs']['7807355364']['royalty']['amount'], 129000)
        self.assertEqual(august['orgs']['7807355364']['royalty']['amount'], 90000)
        self.assertEqual(d['digital_royalty_total']['amount'], 219000)
        self.assertTrue(d['digital_royalty_total']['preliminary'])
        self.assertEqual(d['digital_royalty_total']['months'], ['2026-07', '2026-08'])
        self.assertIsNone(july['orgs']['7811803918']['royalty'])
        self.assertIsNone(september['orgs']['7807355364']['royalty']['amount'])
        self.assertIsNone(october['orgs']['7807355364']['royalty']['amount'])
        self.assertEqual(d['royalty_period'], 'month')



if __name__ == '__main__':
    unittest.main()

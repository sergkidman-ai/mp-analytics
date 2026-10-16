# поток: fin
from copy import deepcopy
from decimal import Decimal
import unittest

from reports import kudir_report, kudir_tax


def quarter(q, income='0.00', expense='0.00', **extra):
    return dict(quarter=q, year=2026, org_inn='test', calculated={'income': income, 'expense': expense}, **extra)


class KudirTaxTests(unittest.TestCase):
    def test_cumulative_loss_carries_forward_within_year(self):
        periods = kudir_tax.calculate_periods([quarter(1, '1000', '2000'), quarter(2, '2000', '0')])
        self.assertEqual(periods[0]['tax'], '0')
        self.assertEqual(periods[1]['tax_base'], '1000.00')
        self.assertEqual(periods[1]['tax'], '150')

    def test_negative_change_is_not_clamped_to_new_tax_payment(self):
        periods = kudir_tax.calculate_periods([quarter(1, '10000', '0'), quarter(2, '0', '5000')])
        self.assertEqual(periods[1]['tax'], '750')
        self.assertEqual(periods[1]['tax_change'], '-750')

    def test_annual_minimum_is_not_applied_to_quarter(self):
        first = kudir_tax.calculate_periods([quarter(1, '100000', '99000')])[0]
        self.assertEqual(first['tax'], '150')
        self.assertIsNone(first['minimum_tax'])
        self.assertFalse(first['minimum_applied'])

    def test_full_year_pays_larger_of_ordinary_and_minimum(self):
        for income, expense, ordinary, expected in [('100000', '99000', '150', '1000'),
                                                     ('100000', '110000', '0', '1000'),
                                                     ('100000', '10000', '13500', '13500')]:
            with self.subTest(expense=expense):
                reports = [quarter(q) for q in (1, 2, 3)] + [quarter(4, income, expense)]
                result = kudir_tax.calculate_periods(reports)[-1]
                self.assertTrue(result['is_annual'])
                self.assertEqual(result['ordinary_tax'], ordinary)
                self.assertEqual(result['tax'], expected)
                self.assertEqual(result['minimum_tax'], '1000')

    def test_partial_fourth_quarter_does_not_impose_annual_minimum(self):
        reports = [quarter(q) for q in (1, 2, 3)] + [quarter(4, '100000', '110000', is_partial=True)]
        result = kudir_tax.calculate_periods(reports)[-1]
        self.assertFalse(result['is_annual'])
        self.assertIsNone(result['minimum_tax'])
        self.assertEqual(result['tax'], '0')
        self.assertEqual(result['minimum_tax_reference'], '1000')

    def test_tax_rounding_is_to_whole_rubles_half_up(self):
        for income, expected in [('3.33', '0'), ('3.34', '1'), ('10.00', '2')]:
            with self.subTest(income=income):
                self.assertEqual(kudir_tax.calculate_periods([quarter(1, income)])[0]['tax'], expected)

    def test_period_integrity(self):
        for reports in [[quarter(2)], [quarter(1), quarter(3)],
                        [quarter(1, is_partial=True), quarter(2)],
                        [quarter(1), dict(quarter(2), year=2025)],
                        [quarter(1), dict(quarter(2), org_inn='another')]]:
            with self.subTest(reports=reports), self.assertRaises(ValueError):
                kudir_tax.calculate_periods(reports)


@unittest.skipUnless(kudir_report.source_path(2).exists(), 'Local accountant source not attached')
class KudirTaxSourceTests(unittest.TestCase):
    def test_cifrovoi_confirmed_income_is_counted_once_in_half_year(self):
        tax = kudir_tax.load('cifrovoi', 2)
        self.assertEqual(tax['current']['income'], '54700500.71')
        self.assertEqual(tax['current']['expense'], '48520612.67')
        self.assertEqual(tax['current']['tax_base'], '6179888.04')
        self.assertEqual(tax['current']['tax'], '926983')
        self.assertEqual(tax['current']['tax_change'], '628180')
        self.assertEqual(tax['income_confirmation']['income'], '1288086.99')
        self.assertFalse(tax['automatic_kudir_enabled'])
        self.assertIsNone(tax['paid_advances'])
        self.assertIsNone(tax['prior_year_losses'])
        self.assertIsNone(kudir_tax.load('cifrovoi', 1)['income_confirmation'])

    def test_confirmation_expires_when_source_or_rows_change(self):
        data = deepcopy(kudir_report.load(2))
        self.assertIsNotNone(kudir_tax.confirmed_income(data))
        data['sha256'] = 'different-source'
        self.assertIsNone(kudir_tax.confirmed_income(data))
        data = deepcopy(kudir_report.load(2))
        data['operations'][next(i for i, o in enumerate(data['operations']) if o['row'] == 322)]['income'] = '1'
        self.assertIsNone(kudir_tax.confirmed_income(data))

    @unittest.skipUnless(kudir_report.source_path(1, 'diskver').exists(), 'Diskver source not attached')
    def test_diskver_nine_months_and_partial_year_are_separate(self):
        tax = kudir_tax.load('diskver', 3)
        self.assertEqual(tax['current']['tax'], '739532')
        self.assertEqual(tax['current']['tax_base'], '4930216.14')
        self.assertFalse(tax['current']['is_partial'])
        year_to_date = kudir_tax.load('diskver', 4)['current']
        self.assertEqual(year_to_date['tax'], '702488')
        self.assertEqual(year_to_date['tax_change'], '-37044')
        self.assertTrue(year_to_date['is_partial'])
        self.assertFalse(year_to_date['is_annual'])
        self.assertEqual(year_to_date['through'], '2026-10-13')

    def test_tax_in_api_and_page_preserves_accountant_source(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.kudir_api import router
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.get('/api/reports/kudir?org=cifrovoi&quarter=2')
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data['accountant']['income'], '27359773.35')
            self.assertEqual(data['delta']['income'], '1288086.99')
            self.assertEqual(data['income_confirmation']['status'], 'confirmed_omitted')
            self.assertEqual(data['tax']['current']['tax'], '926983')
            page = client.get('/reports/kudir?org=cifrovoi&quarter=2').text
            self.assertIn('id="k-tax"', page)
            self.assertIn('926 983,00 ₽', page)
            self.assertIn('учтёт три доходные строки в годовом отчёте', page)
            self.assertNotIn('Учёт этих сумм в расчёте налога по двум файлам установить нельзя', page)
            self.assertIn('Автоматическое ведение КУДиР отложено', page)
            self.assertNotIn('получить расшифровку 1 288 086', page)


if __name__ == '__main__':
    unittest.main()

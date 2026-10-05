# поток: inv
"""Регрессия PDF-парсера Феррета; без обработки документов и вызовов API."""
import unittest

from invoice_bot.invoice_to_po import parse_pdf_ferret


class FerretPdfTests(unittest.TestCase):
    def test_missing_unit_keeps_article_quantity_and_amount_aligned(self):
        text = (
            "1 S24199174609 CS-PH7100BK 100,00 2 шт 200,00\n"
            "2 2045744 875,47 1 875,47\n"
            "Картридж CS-D111L-MPSXL\n"
            "3 2045745 CSP-W2030XSET 500,00 3 шт 1500,00\n"
        )
        items = parse_pdf_ferret(text)
        self.assertEqual(
            [(r['num'], r['art_raw'], r['qty'], r['price'], r['sum']) for r in items],
            [(1, 'CS-PH7100BK', 2, 100, 200),
             (2, 'CS-D111L-MPSXL', 1, 875.47, 875.47),
             (3, 'CSP-W2030XSET', 3, 500, 1500)],
        )

    def test_existing_units_and_article_formats(self):
        text = (
            "1 100 CS-PGI2400BK/C/M/Y 10,00 2 шт 20,00\n"
            "2 101 GG-H218X 20,00 1 шт 20,00\n"
            "3 102 CR-123 30,00 1 шт 30,00\n"
        )
        items = parse_pdf_ferret(text)
        self.assertEqual([r['art_raw'] for r in items],
                         ['CS-PGI2400BK/C/M/Y', 'GG-H218X', 'CR-123'])
        self.assertEqual(sum(r['sum'] for r in items), 70)

    def test_unknown_unit_still_stops(self):
        with self.assertRaisesRegex(SystemExit, 'рассинхрон'):
            parse_pdf_ferret("1 100 CS-123 10,00 2 упак 20,00\n")

    def test_incomplete_amount_still_stops(self):
        with self.assertRaisesRegex(SystemExit, 'рассинхрон'):
            parse_pdf_ferret("1 100 CS-123 10,00 2\n")

    def test_missing_article_still_stops(self):
        with self.assertRaisesRegex(SystemExit, 'рассинхрон'):
            parse_pdf_ferret("1 100 неизвестный 10,00 2 20,00\n")


if __name__ == '__main__':
    unittest.main()

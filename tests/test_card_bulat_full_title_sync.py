import unittest
import xml.etree.ElementTree as ET
from tools.card_bulat_full_title_sync import extract_title, index_offers, APPROVED_TITLES


class FullTitleTests(unittest.TestCase):
    def test_preserves_chip_and_resource_suffix(self):
        title = 'Тонер-картридж e-Line TK-6305 (Чёрный, 35000 стр.), с чипом'
        self.assertEqual(extract_title('\n' + title + '   Тонер-картридж предназначен для печати.'), (title, ''))

    def test_prose_intro_is_not_a_product_title(self):
        text = 'Для организаций, которые регулярно печатают, картридж позволяет снизить расходы.  Основные характеристики'
        self.assertFalse(extract_title(text)[0])
        self.assertFalse(extract_title('Тонер-картридж Smart Graphics — практичное решение для печати.  Совместимость')[0])

    def test_short_incomplete_or_empty_source_is_rejected(self):
        self.assertFalse(extract_title('Тонер-картридж  для Canon с чипом')[0])
        self.assertFalse(extract_title('')[0])
        self.assertFalse(extract_title('Тонер-картридж для HP и Для устройств: HP  Более длинное описание')[0])

    def test_single_plain_title_is_supported(self):
        title = 'Тонер-картридж БУЛАТ s-Line TK-4145 (16000 стр.) с чипом'
        self.assertEqual(extract_title(title), (title, ''))

    def test_zero_stock_offer_remains_matchable(self):
        root = ET.fromstring('<yml_catalog><offer id="SKU"><vendorCode>123</vendorCode><count>0</count></offer></yml_catalog>')
        index = index_offers(root)
        self.assertIn('SKU', index['SKU'])
        self.assertIn('SKU', index['123'])

    def test_approved_titles_are_scoped_to_specific_products(self):
        self.assertEqual(len(APPROVED_TITLES), 2)
        self.assertTrue(APPROVED_TITLES[('3779', 'AFBRHL2350030')].endswith(', с чипом'))
        self.assertTrue(APPROVED_TITLES[('5777', 'AFBRHL2350020')].endswith(', с чипом'))
        self.assertNotIn(('3779', 'OTHER-SUPPLIER-SKU'), APPROVED_TITLES)


if __name__ == '__main__':
    unittest.main()

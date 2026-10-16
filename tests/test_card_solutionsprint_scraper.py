import unittest

from tools.card_solutionsprint_scraper import parse_card, allowed_url


def page(stock='В наличии более 10 шт.', table_extra=''):
    return f'''<html><h1>Картридж SP W2031A голубой (без чипа)</h1>
    <div data-entity="main-product-wrap">
      <div class="item-code">Код товара: 290096</div>
      <div class="product-page">
        <span class="label-item without-chip">Без чипа</span>
        <div class="product-availability">{stock}</div>
        <div class="comments-to-product">Чип переставить с оригинала.</div>
      </div>
      <div class="recommended-anchor-wrap">
        <span class="analog__content">ЧУЖОЙ АНАЛОГ CF289A</span>
        <div class="product-availability">В наличии 999 шт.</div>
      </div>
      <table class="characteristics-table">
        <tr><td>Аналог</td><td>HP W2031A</td></tr>
        <tr><td>Цвет</td><td>Голубой</td></tr>
        <tr><td>Ресурс</td><td>2100 страниц</td></tr>
        <tr><td>Вид товара</td><td>Картридж лазерный</td></tr>
      </table>{table_extra}
    </div></html>'''


class SolutionsPrintParserTests(unittest.TestCase):
    def parse(self, content):
        return parse_card(content, 'https://cartridge.ru/catalog/example/')

    def test_main_properties_and_stock_do_not_take_related_product(self):
        r = self.parse(page())
        self.assertEqual(r['analog'], 'HP W2031A')
        self.assertEqual(r['supplier_code'], '290096')
        self.assertEqual(r['chip_badges'], ['Без чипа'])
        self.assertEqual(r['stock_quantity'], 10)
        self.assertTrue(r['quantity_is_lower_bound'])

    def test_under_order_is_not_positive_even_with_related_stock(self):
        r = self.parse(page('Под заказ ~ 14.10.2026'))
        self.assertFalse(r['stock_positive'])
        self.assertIsNone(r['stock_quantity'])

    def test_unknown_availability_is_not_assumed_positive(self):
        r = self.parse(page('Уточните у менеджера'))
        self.assertIsNone(r['stock_positive'])

    def test_private_stock_preserves_both_warehouses_without_first_as_total(self):
        r = self.parse(page('В наличии Склад Спб - 905 шт. Склад Мск - 437 шт.'))
        self.assertEqual(r['warehouse_stocks'], {'Спб':905, 'Мск':437})
        self.assertIsNone(r['stock_quantity'])
        self.assertTrue(r['stock_positive'])

    def test_named_warehouses_zero_are_not_positive(self):
        r = self.parse(page('В наличии Склад Спб - 0 шт. Склад Мск - 0 шт.'))
        self.assertFalse(r['stock_positive'])

    def test_ambiguous_property_table_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(page(table_extra='<table class="characteristics-table"><tr><td>Аналог</td><td>Чужой</td></tr></table>'))

    def test_other_supplier_or_tokenized_url_is_rejected(self):
        for url in ('https://thecartridge.ru/catalog/test/', 'https://cartridge.ru/catalog/test/?token=secret'):
            with self.assertRaises(ValueError):
                allowed_url(url)


if __name__ == '__main__':
    unittest.main()

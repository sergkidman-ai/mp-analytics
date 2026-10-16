import unittest

from lxml import html
from core import ms_api
from tools.card_solutionsprint_description_audit import IDS, supplier_code, listing, max_page


class SolutionsPrintAuditTests(unittest.TestCase):
    def card(self, group, article):
        sid = next(i for i, g in IDS.items() if g == group)
        return {'article':article, 'supplier':{'meta':{'href':ms_api.BASE+'/entity/counterparty/'+sid}}}

    def test_mapping_preserves_supplier_legal_entity_suffix(self):
        self.assertEqual(supplier_code(self.card('Спринт','SP304437')),'304437')
        self.assertEqual(supplier_code(self.card('Спринт МСК','SP304437_MSK')),'304437')
        self.assertEqual(supplier_code(self.card('Спринт','SP304437_MSK')),'')
        self.assertEqual(supplier_code(self.card('Спринт МСК','SPП022985_MSK')),'')

    def test_listing_pairs_code_with_title_inside_one_item_only(self):
        doc = html.fromstring('''<div>
        <a href="/catalog/unrelated/">Чужой товар</a>
        <div data-entity="item"><div class="product-cart__item-code">Код товара: 304437</div>
        <div class="product-cart__title"><a href="/catalog/right/">Картридж SP 101S черный</a></div></div>
        <div data-entity="item"><div class="product-cart__item-code">Код товара: 261637</div></div>
        </div>''')
        rows=listing(doc)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['supplier_code'],'304437')
        self.assertEqual(rows[0]['url'],'https://cartridge.ru/catalog/right/')

    def test_pagination_reads_last_page_from_links_not_only_next(self):
        doc=html.fromstring('<div><a href="?PAGEN_1=2">2</a><a href="?PAGEN_1=268">268</a></div>')
        self.assertEqual(max_page(doc),268)


if __name__=='__main__':
    unittest.main()

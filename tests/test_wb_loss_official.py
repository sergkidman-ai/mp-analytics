# поток: ev
from collections import Counter
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from ops.wb_loss_official import Catalogue, parse_pdf

class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.c=Catalogue(['0002','5336','1111'],{'9000':['0002','0002','1111'],
            '9001':['9000','1111'],'9002':["{'external_code': '5336', 'quantity': 2}"]},[('0002','5336')])
    def test_numeric_derivative_00191_uses_base_0019(self):
        c=Catalogue(['0019'],{},[])
        self.assertEqual(c.code('00191'),'0019')
        self.assertEqual(c.expand('00191'),Counter({'0019':1}))
        self.assertEqual(c.signature(c.expand('00191')),c.signature(c.expand('0019')))
    def test_equivalent_actual_shipment(self):
        self.assertEqual(self.c.signature(Counter({'0002':1})),self.c.signature(Counter({'5336':1})))
    def test_set_quantities_not_inferred_from_suffix(self):
        self.assertEqual(self.c.expand('90002'),Counter({'0002':2,'1111':1}))
        with self.assertRaises(ValueError):self.c.expand('0002X3')
    def test_random_alphanumeric_suffix_is_not_quantity(self):
        c=Catalogue(['6414','1530','1531'],{'0064':['1530','1531']},[])
        self.assertEqual(c.expand('6414X1XF7NYJ'),Counter({'6414':1}))
        self.assertEqual(c.expand('0064X9775UHA'),Counter({'1530':1,'1531':1}))
        with self.assertRaises(ValueError):c.expand('6414X3')
    def test_nested_and_dictionary_composition(self):
        self.assertEqual(self.c.expand('9001'),Counter({'0002':2,'1111':2}))
        self.assertEqual(self.c.expand('9002'),Counter({'5336':2}))
    def test_incomplete_or_extra_components_do_not_match(self):
        sig=self.c.signature(self.c.expand('9000'))
        self.assertNotEqual(sig,self.c.signature(Counter({'5336':1,'1111':1})))
        self.assertNotEqual(sig,self.c.signature(Counter({'5336':2,'1111':2})))
        self.assertEqual(sig,self.c.signature(Counter({'5336':2,'1111':1})))
    def test_cycle_rejected(self):
        c=Catalogue([],{'9000':['9001'],'9001':['9000']},[])
        with self.assertRaises(ValueError):c.expand('9000')

class ParserTests(unittest.TestCase):
    def parse(self,rows):
        text='Отчет по утраченному товару\nот 29.08.2026\nИНН 7807355364 / 771701001\nпо складу Тула\nАртикул Продавца\n'+rows
        with patch('ops.wb_loss_official.subprocess.run',return_value=SimpleNamespace(stdout=text.encode())):
            return parse_pdf(b'pdf')
    def test_leading_zero_code_and_page_provenance(self):
        wh,date,items=self.parse('Товар  1002561300  00022  2\n\fТовар  123456789  9000  1')
        self.assertEqual(wh,'Тула');self.assertEqual(items[0]['vendor_code'],'00022')
        self.assertEqual(items[1]['source_pages'],[2]);self.assertEqual(sum(i['qty'] for i in items),3)
    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):self.parse('Товар  123456789  0002  1\nТовар  123456789  0002  2')
    def test_malformed_numeric_row_rejected(self):
        with self.assertRaises(ValueError):self.parse('Товар  123456789  0002  неясно')

if __name__=='__main__':unittest.main()
